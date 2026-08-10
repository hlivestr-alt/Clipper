"""WhatsApp delivery media policy, probing, classification, and validation.

This module is intentionally independent of the advertising/content compliance
checker.  It describes whether an MP4 is safe to publish into the permanent
WhatsApp batch mirror and supplies the common policy used by the renderer and
backlog processor.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from enum import Enum
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable


POLICY_REVISION = "whatsapp-media-v4-clipper-stale-nclx-cohort"
STALE_NCLX_POLICY_ID = "clipper-stale-nclx-v2-r191-r200-broll-v1"

# The remaining conflicts are not an arbitrary collection of files.  The
# source ledger shows one stable Clipper/NVENC production family spanning
# runs 191 through 200, always variation 4 (b-roll-only).  Keep the family
# bounded and explicit so a future external file cannot match on colour tags
# alone.
APPROVED_STALE_NCLX_RUN_NUMBERS = frozenset(range(191, 201))
_AMBIENT_VIEWING_ENVIRONMENT = {
    "ambient_illuminance": "3140000/10000",
    "ambient_light_x": "15635/50000",
    "ambient_light_y": "16450/50000",
}


# These two immutable sources were inspected frame-by-frame for the controlled
# batch-6901 pilot.  Their MP4 nclx boxes contradict their H.264 SPS/VUI: the
# container says full-range GBR while the codec says limited-range BT.470BG.
# Both are already visually SDR after composition.  Matching the content hash
# as well as the complete observed signature keeps the global conflict policy
# fail-closed for every other file.
APPROVED_COLOR_OVERRIDES: dict[str, dict[str, Any]] = {
    "fca40edd24538166d1e9e39151fd5187659bb2c95c6d50b59b998d14a6bf410f": {
        "override_id": "batch6901-eye-cream-v4-sdr-vui",
        "signature": {
            "size_bytes": 31_299_259,
            "video_codec": "h264",
            "video_encoder": "Lavc62.29.101 h264_nvenc",
            "pixel_format": "yuv420p",
            "bits_per_raw_sample": 8,
            "width": 1080,
            "height": 1920,
            "color_range": "pc",
            "color_space": "gbr",
            "color_primaries": "bt709",
            "color_transfer": "bt709",
        },
        "codec_vui": {
            "color_range": "limited",
            "color_space": "bt470bg",
            "color_primaries": "bt709",
            "color_transfer": "bt709",
        },
    },
    "fbc8bd393857842f89877f802a4bc1f85e4d9226347e966cce423847b8fe6be8": {
        "override_id": "batch6901-cleanser-v4-sdr-vui",
        "signature": {
            "size_bytes": 26_586_552,
            "video_codec": "h264",
            "video_encoder": "Lavc62.29.101 h264_nvenc",
            "pixel_format": "yuv420p",
            "bits_per_raw_sample": 8,
            "width": 1080,
            "height": 1920,
            "color_range": "pc",
            "color_space": "gbr",
            "color_primaries": "bt2020",
            "color_transfer": "arib-std-b67",
        },
        "codec_vui": {
            "color_range": "limited",
            "color_space": "bt470bg",
            "color_primaries": "bt2020",
            "color_transfer": "arib-std-b67",
        },
    },
}
MP4_SUFFIXES = {".mp4", ".m4v"}
NORMAL_FRAME_RATES = (
    Fraction(24_000, 1_001),
    Fraction(24, 1),
    Fraction(25, 1),
    Fraction(30_000, 1_001),
    Fraction(30, 1),
)


class ProcessingAction(str, Enum):
    COPIED = "copied"
    REMUXED = "remuxed"
    BACKLOG_TRANSCODED = "backlog_transcoded"
    DIRECT_RENDERED = "direct_rendered"
    RETRY_RENDERED = "retry_rendered"
    FALLBACK_TRANSCODED = "fallback_transcoded"


class Classification(str, Enum):
    COPY = "copy"
    REMUX = "remux"
    TRANSCODE = "transcode"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class MediaPolicy:
    revision: str = POLICY_REVISION
    max_bytes: int = 16_000_000
    target_bytes: int = 15_000_000
    audio_bitrate_bps: int = 96_000
    max_video_bitrate_bps: int = 8_000_000
    max_fps: Fraction = Fraction(30, 1)
    gop_seconds: float = 2.0
    threshold_720p_bps: int = 3_500_000
    max_encode_attempts: int = 3
    nvenc_preset: str = "p6"
    nvenc_tune: str = "hq"
    nvenc_multipass: str = "fullres"
    output_color_range: str = "tv"
    output_color_space: str = "bt709"
    output_color_primaries: str = "bt709"
    output_color_transfer: str = "bt709"

    @classmethod
    def from_config(cls, cfg: Any) -> "MediaPolicy":
        audio = str(getattr(cfg, "WHATSAPP_AUDIO_BITRATE", "96k")).strip().lower()
        if audio.endswith("k"):
            audio_bps = int(float(audio[:-1]) * 1_000)
        else:
            audio_bps = int(audio)
        policy = cls(
            revision=str(getattr(cfg, "WHATSAPP_POLICY_REVISION", POLICY_REVISION)),
            max_bytes=int(getattr(cfg, "WHATSAPP_MAX_BYTES", 16_000_000)),
            target_bytes=int(getattr(cfg, "WHATSAPP_TARGET_BYTES", 15_000_000)),
            audio_bitrate_bps=audio_bps,
            max_video_bitrate_bps=int(
                getattr(cfg, "WHATSAPP_MAX_VIDEO_BITRATE_BPS", 8_000_000)
            ),
            max_fps=Fraction(str(getattr(cfg, "WHATSAPP_MAX_FPS", 30))),
            gop_seconds=float(getattr(cfg, "WHATSAPP_GOP_SECONDS", 2.0)),
            threshold_720p_bps=int(
                getattr(cfg, "WHATSAPP_720P_THRESHOLD_BPS", 3_500_000)
            ),
            max_encode_attempts=int(
                getattr(cfg, "WHATSAPP_MAX_ENCODE_ATTEMPTS", 3)
            ),
            nvenc_preset=str(getattr(cfg, "WHATSAPP_NVENC_PRESET", "p6")),
            nvenc_tune=str(getattr(cfg, "WHATSAPP_NVENC_TUNE", "hq")),
            nvenc_multipass=str(
                getattr(cfg, "WHATSAPP_NVENC_MULTIPASS", "fullres")
            ),
        )
        policy.validate()
        return policy

    def validate(self) -> None:
        if not 0 < self.target_bytes < self.max_bytes:
            raise ValueError("WHATSAPP_TARGET_BYTES must be below WHATSAPP_MAX_BYTES")
        if self.audio_bitrate_bps < 0 or self.max_video_bitrate_bps <= 0:
            raise ValueError("WhatsApp bitrates must be positive")
        if self.max_fps <= 0 or self.gop_seconds <= 0:
            raise ValueError("WhatsApp FPS and GOP settings must be positive")
        if self.max_encode_attempts < 1:
            raise ValueError("WHATSAPP_MAX_ENCODE_ATTEMPTS must be at least one")

    def fingerprint(self) -> str:
        payload = asdict(self)
        payload["max_fps"] = str(self.max_fps)
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


@dataclass(frozen=True)
class BitratePlan:
    expected_duration_seconds: float
    container_reserve_bytes: int
    retry_reserve_bytes: int
    audio_budget_bytes: int
    target_video_bps: int
    maxrate_bps: int
    bufsize_bps: int


@dataclass
class MediaProbe:
    path: str
    size_bytes: int = 0
    format_name: str = ""
    format_long_name: str = ""
    major_brand: str | None = None
    compatible_brands: str | None = None
    duration_seconds: float | None = None
    faststart: bool = False
    video_stream_count: int = 0
    audio_stream_count: int = 0
    subtitle_stream_count: int = 0
    data_stream_count: int = 0
    primary_video_stream_index: int | None = None
    primary_audio_stream_index: int | None = None
    primary_stream_ambiguous: bool = False
    video_codec: str | None = None
    video_encoder: str | None = None
    muxer_encoder: str | None = None
    video_profile: str | None = None
    h264_level: int | None = None
    has_b_frames: int | None = None
    pixel_format: str | None = None
    width: int | None = None
    height: int | None = None
    sample_aspect_ratio: str | None = None
    field_order: str | None = None
    r_frame_rate: str | None = None
    avg_frame_rate: str | None = None
    source_frame_rate_mode: str = "unknown"
    rotation: int = 0
    color_range: str | None = None
    color_space: str | None = None
    color_primaries: str | None = None
    color_transfer: str | None = None
    chroma_location: str | None = None
    bits_per_raw_sample: int | None = None
    codec_vui: dict[str, Any] = field(default_factory=dict)
    decoded_frame_color: dict[str, Any] = field(default_factory=dict)
    hdr_side_data: list[dict[str, Any]] = field(default_factory=list)
    mastering_display_metadata: list[dict[str, Any]] = field(default_factory=list)
    content_light_metadata: list[dict[str, Any]] = field(default_factory=list)
    production_provenance: dict[str, Any] = field(default_factory=dict)
    base_clip_identity: str | None = None
    hdr: bool = False
    color_conflict: bool = False
    source_sha256: str | None = None
    color_policy_override: dict[str, Any] | None = None
    audio_codec: str | None = None
    audio_profile: str | None = None
    audio_channels: int | None = None
    audio_channel_layout: str | None = None
    audio_sample_rate: int | None = None
    chapters: int = 0
    attached_pictures: int = 0
    probe_error: str | None = None
    full_decode_passed: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ClassificationResult:
    classification: Classification
    reasons: tuple[str, ...]
    probe: MediaProbe


@dataclass
class DeliveryComplianceResult:
    compliant: bool
    policy_revision: str
    action: str
    diagnostics: dict[str, Any] = field(default_factory=dict)
    failure_codes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RenderResult:
    ok: bool
    output_path: Path | None
    delivery_compliance: DeliveryComplianceResult | None
    failure_stage: str | None = None
    error_code: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["output_path"] = str(self.output_path) if self.output_path else None
        return payload


def _fraction(value: str | None) -> Fraction | None:
    if not value or value in {"0/0", "N/A"}:
        return None
    try:
        parsed = Fraction(value)
    except (ValueError, ZeroDivisionError):
        return None
    return parsed if parsed > 0 else None


def choose_final_fps(
    r_frame_rate: str | None,
    avg_frame_rate: str | None,
    *,
    max_fps: Fraction = Fraction(30, 1),
) -> Fraction:
    nominal = _fraction(r_frame_rate)
    average = _fraction(avg_frame_rate)
    source = average or nominal or Fraction(30, 1)
    if source > max_fps:
        return max_fps
    for normal in NORMAL_FRAME_RATES:
        if abs(float(source - normal)) <= 0.02:
            return normal
    return source


def calculate_bitrate_plan(
    duration_seconds: float,
    *,
    has_audio: bool,
    policy: MediaPolicy,
    source_video_bps: int | None = None,
) -> BitratePlan:
    duration = max(float(duration_seconds), 0.25)
    container_reserve = max(256_000, math.ceil(policy.target_bytes * 0.01))
    retry_reserve = max(128_000, math.ceil(policy.target_bytes * 0.005))
    audio_budget = (
        math.ceil(policy.audio_bitrate_bps * duration / 8) + 32_768
        if has_audio
        else 0
    )
    video_budget = max(
        1, policy.target_bytes - container_reserve - retry_reserve - audio_budget
    )
    target = math.floor(video_budget * 8 / duration)
    target = min(target, policy.max_video_bitrate_bps)
    if source_video_bps and source_video_bps > 0:
        target = min(target, math.floor(source_video_bps * 1.05))
    target = max(32_000, target)
    return BitratePlan(
        expected_duration_seconds=duration,
        container_reserve_bytes=container_reserve,
        retry_reserve_bytes=retry_reserve,
        audio_budget_bytes=audio_budget,
        target_video_bps=target,
        maxrate_bps=max(target, math.floor(target * 1.15)),
        bufsize_bps=max(target, target * 2),
    )


def retry_bitrate(previous_bps: int, actual_bytes: int, policy: MediaPolicy) -> int:
    if actual_bytes <= 0:
        return previous_bps
    return max(
        32_000,
        math.floor(previous_bps * (policy.target_bytes / actual_bytes) * 0.97),
    )


def _faststart(path: Path) -> bool:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            offset = 0
            moov_offset: int | None = None
            mdat_offset: int | None = None
            while offset + 8 <= size:
                handle.seek(offset)
                header = handle.read(8)
                if len(header) != 8:
                    break
                box_size = int.from_bytes(header[:4], "big")
                box_type = header[4:8]
                header_size = 8
                if box_size == 1:
                    extended = handle.read(8)
                    if len(extended) != 8:
                        break
                    box_size = int.from_bytes(extended, "big")
                    header_size = 16
                elif box_size == 0:
                    box_size = size - offset
                if box_size < header_size or offset + box_size > size:
                    return False
                if box_type == b"moov":
                    moov_offset = offset
                elif box_type == b"mdat":
                    mdat_offset = offset
                if moov_offset is not None and mdat_offset is not None:
                    return moov_offset < mdat_offset
                offset += box_size
        return moov_offset is not None and (
            mdat_offset is None or moov_offset < mdat_offset
        )
    except (OSError, ValueError):
        return False


def _rotation(stream: dict[str, Any]) -> int:
    tags = stream.get("tags") or {}
    try:
        tagged = int(round(float(tags.get("rotate", 0)))) % 360
    except (TypeError, ValueError):
        tagged = 0
    for item in stream.get("side_data_list") or []:
        if "rotation" in item:
            try:
                return int(round(float(item["rotation"]))) % 360
            except (TypeError, ValueError):
                pass
    return tagged


def _is_hdr(stream: dict[str, Any]) -> bool:
    transfer = str(stream.get("color_transfer") or "").casefold()
    primaries = str(stream.get("color_primaries") or "").casefold()
    space = str(stream.get("color_space") or "").casefold()
    pix_fmt = str(stream.get("pix_fmt") or "").casefold()
    side_names = " ".join(
        str(item.get("side_data_type") or "") for item in stream.get("side_data_list") or []
    ).casefold()
    return (
        transfer in {"arib-std-b67", "smpte2084"}
        or "bt2020" in {primaries, space}
        or any(token in pix_fmt for token in ("p10", "p12"))
        or "mastering display" in side_names
        or "content light" in side_names
    )


def _color_conflict(stream: dict[str, Any]) -> bool:
    transfer = str(stream.get("color_transfer") or "").casefold()
    primaries = str(stream.get("color_primaries") or "").casefold()
    space = str(stream.get("color_space") or "").casefold()
    hdr_transfer = transfer in {"arib-std-b67", "smpte2084"}
    if hdr_transfer and primaries and primaries != "bt2020":
        return True
    if space == "gbr" and str(stream.get("pix_fmt") or "").startswith("yuv"):
        return True
    return False


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


_H264_PRIMARIES = {1: "bt709", 9: "bt2020"}
_H264_TRANSFERS = {1: "bt709", 18: "arib-std-b67"}
_H264_MATRICES = {1: "bt709", 5: "bt470bg", 9: "bt2020nc"}
_CLIPPER_SOURCE_NAME = re.compile(
    r"^(?P<run>.+_run_(?P<run_number>\d+))__"
    r"(?P=run)_clip_(?P<clip>\d{4})_v(?P<variant>\d+)_"
    r"(?P<kind>.+?)_score\d+_",
    re.IGNORECASE,
)


def _production_provenance(path: Path) -> dict[str, Any]:
    match = _CLIPPER_SOURCE_NAME.match(path.stem)
    parent_is_batch = path.parent.name.isdigit()
    root_is_export_batches = path.parent.parent.name.casefold() == "export_batches"
    if not match:
        return {"recognized": False}
    values = match.groupdict()
    base = f"{values['run']}_clip_{values['clip']}"
    return {
        "recognized": bool(parent_is_batch and root_is_export_batches),
        "batch_number": int(path.parent.name) if parent_is_batch else None,
        "run_identity": values["run"],
        "run_number": int(values["run_number"]),
        "base_clip_identity": base,
        "variation_index": int(values["variant"]),
        "variation_kind": values["kind"].casefold(),
    }


def _codec_vui(path: Path) -> dict[str, Any]:
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "verbose", "-i", str(path),
        "-map", "0:v:0", "-c", "copy", "-bsf:v", "trace_headers",
        "-frames:v", "1", "-f", "null", "NUL" if os.name == "nt" else "/dev/null",
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=60, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    trace = completed.stderr or ""

    def value(name: str) -> int | None:
        found = re.search(rf"{name}\s+[01]+\s*=\s*(\d+)", trace)
        return int(found.group(1)) if found else None

    full_range = value("video_full_range_flag")
    primaries = value("colour_primaries")
    transfer = value("transfer_characteristics")
    matrix = value("matrix_coefficients")
    if all(item is None for item in (full_range, primaries, transfer, matrix)):
        return {}
    return {
        "color_range": {0: "limited", 1: "full"}.get(full_range),
        "color_space": _H264_MATRICES.get(matrix, f"h264-value-{matrix}"),
        "color_primaries": _H264_PRIMARIES.get(
            primaries, f"h264-value-{primaries}"
        ),
        "color_transfer": _H264_TRANSFERS.get(
            transfer, f"h264-value-{transfer}"
        ),
        "video_full_range_flag": full_range,
        "matrix_coefficients": matrix,
        "colour_primaries": primaries,
        "transfer_characteristics": transfer,
    }


def _decoded_frame_metadata(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    command = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-read_intervals", "0%+0.1", "-show_frames", "-show_entries",
        "frame=pix_fmt,color_range,color_space,color_primaries,color_transfer,side_data_list",
        "-of", "json", str(path),
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=60, check=False
        )
        frames = json.loads(completed.stdout).get("frames") or []
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return {}, []
    if not frames:
        return {}, []
    frame = frames[0]
    color = {
        key: frame.get(key)
        for key in (
            "pix_fmt", "color_range", "color_space", "color_primaries",
            "color_transfer",
        )
    }
    return color, list(frame.get("side_data_list") or [])


def _dedupe_side_data(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        key = json.dumps(item, sort_keys=True, separators=(",", ":"))
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def _override_payload(
    probe: MediaProbe, *, override_id: str, decision_source: str,
    matched_signature: dict[str, Any],
) -> dict[str, Any]:
    return {
        "override_id": override_id,
        "override_policy_id": STALE_NCLX_POLICY_ID,
        "decision_source": decision_source,
        "metadata_overridden": True,
        "original_container_stream_tags": {
            "pixel_format": probe.pixel_format,
            "color_range": probe.color_range,
            "color_space": probe.color_space,
            "color_primaries": probe.color_primaries,
            "color_transfer": probe.color_transfer,
        },
        "codec_vui": dict(probe.codec_vui),
        "decoded_frame_properties": dict(probe.decoded_frame_color),
        "hdr_side_data": list(probe.hdr_side_data),
        "mastering_display_metadata": list(probe.mastering_display_metadata),
        "content_light_metadata": list(probe.content_light_metadata),
        "production_provenance": dict(probe.production_provenance),
        "matched_production_signature": matched_signature,
        "selected_interpretation": "limited_range_bt470bg_matrix_sdr_bt709",
        "conversion_path": "limited_bt470bg_sdr_to_limited_bt709",
        "input_range": "limited",
        "input_matrix": "bt470bg",
        "input_primaries": "bt709",
        "input_transfer": "bt709",
        "output_range": "limited",
        "output_matrix": "bt709",
        "output_primaries": "bt709",
        "output_transfer": "bt709",
    }


def _matches_stale_nclx_signature(probe: MediaProbe) -> dict[str, Any] | None:
    container = (
        probe.color_range, probe.color_space, probe.color_primaries,
        probe.color_transfer,
    )
    allowed_containers = {
        ("pc", "gbr", "bt709", "bt709"),
        ("pc", "gbr", "bt2020", "arib-std-b67"),
    }
    if container not in allowed_containers:
        return None
    expected_vui = {
        "color_range": "limited",
        "color_space": "bt470bg",
        "color_primaries": probe.color_primaries,
        "color_transfer": probe.color_transfer,
    }
    if any(probe.codec_vui.get(key) != value for key, value in expected_vui.items()):
        return None
    expected_frame = {
        "pix_fmt": "yuv420p",
        "color_range": "tv",
        "color_space": "bt470bg",
        "color_primaries": probe.color_primaries,
        "color_transfer": probe.color_transfer,
    }
    if any(
        probe.decoded_frame_color.get(key) != value
        for key, value in expected_frame.items()
    ):
        return None
    provenance = probe.production_provenance
    if not (
        provenance.get("recognized") is True
        and provenance.get("run_number") in APPROVED_STALE_NCLX_RUN_NUMBERS
        and provenance.get("variation_index") == 4
        and provenance.get("variation_kind") == "b_roll_only"
    ):
        return None
    if (
        probe.video_codec != "h264"
        or probe.video_profile != "Main"
        or probe.h264_level != 40
        or probe.pixel_format != "yuv420p"
        or probe.bits_per_raw_sample != 8
        or (probe.width, probe.height) != (1080, 1920)
        or probe.r_frame_rate != "30/1"
        or probe.avg_frame_rate != "30/1"
        or probe.source_frame_rate_mode != "cfr"
        or probe.chroma_location != "left"
        or probe.sample_aspect_ratio != "1:1"
        or probe.field_order != "progressive"
        or probe.rotation != 0
        or probe.has_b_frames != 2
        or probe.video_encoder != "Lavc62.29.101 h264_nvenc"
        or probe.muxer_encoder != "Lavf62.13.102"
    ):
        return None
    if probe.mastering_display_metadata or probe.content_light_metadata:
        return None
    side_types = {
        str(item.get("side_data_type") or "") for item in probe.hdr_side_data
    }
    if probe.color_transfer == "bt709":
        if probe.hdr_side_data:
            return None
        side_data_pattern = "none"
    else:
        if not probe.hdr_side_data:
            side_data_pattern = "none"
        elif len(probe.hdr_side_data) == 1:
            item = probe.hdr_side_data[0]
            observed_ambient = {
                key: str(item.get(key) or "")
                for key in _AMBIENT_VIEWING_ENVIRONMENT
            }
            if (
                item.get("side_data_type") == "Ambient viewing environment"
                and observed_ambient == _AMBIENT_VIEWING_ENVIRONMENT
            ):
                side_data_pattern = "ambient_viewing_environment"
            else:
                return None
        else:
            return None
    return {
        "container_signature": list(container),
        "codec_vui": expected_vui,
        "decoded_frame": expected_frame,
        "encoder": probe.video_encoder,
        "muxer": probe.muxer_encoder,
        "dimensions": [probe.width, probe.height],
        "frame_rate": probe.avg_frame_rate,
        "profile": probe.video_profile,
        "level": probe.h264_level,
        "chroma_location": probe.chroma_location,
        "hdr_side_data_types": sorted(side_types),
        "hdr_side_data_pattern": side_data_pattern,
        "provenance": provenance,
    }


def resolve_approved_color_override(probe: MediaProbe) -> dict[str, Any] | None:
    """Resolve an exact audit hash or the narrow audited Clipper signature."""
    if not probe.color_conflict or not probe.source_sha256:
        return None
    approved = APPROVED_COLOR_OVERRIDES.get(probe.source_sha256.casefold())
    observed = {
        "size_bytes": probe.size_bytes,
        "video_codec": probe.video_codec,
        "video_encoder": probe.video_encoder,
        "pixel_format": probe.pixel_format,
        "bits_per_raw_sample": probe.bits_per_raw_sample,
        "width": probe.width,
        "height": probe.height,
        "color_range": probe.color_range,
        "color_space": probe.color_space,
        "color_primaries": probe.color_primaries,
        "color_transfer": probe.color_transfer,
    }
    if approved and observed == approved["signature"]:
        return _override_payload(
            probe,
            override_id=approved["override_id"],
            decision_source="exact_sha256_allowlist",
            matched_signature={
                "sha256": probe.source_sha256,
                "legacy_full_metadata_signature": observed,
            },
        )
    matched = _matches_stale_nclx_signature(probe)
    if not matched:
        return None
    return _override_payload(
        probe,
        override_id=STALE_NCLX_POLICY_ID,
        decision_source="reusable_source_signature",
        matched_signature=matched,
    )


def probe_media(path: str | Path) -> MediaProbe:
    media_path = Path(path)
    probe = MediaProbe(path=str(media_path))
    try:
        probe.size_bytes = media_path.stat().st_size
    except OSError as exc:
        probe.probe_error = str(exc)
        return probe
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_format",
        "-show_streams",
        "-show_chapters",
        "-of",
        "json",
        str(media_path),
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=60, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        probe.probe_error = str(exc)
        return probe
    if completed.returncode:
        probe.probe_error = (completed.stderr or "ffprobe failed").strip()
        return probe
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        probe.probe_error = f"invalid ffprobe JSON: {exc}"
        return probe

    format_info = payload.get("format") or {}
    probe.format_name = str(format_info.get("format_name") or "")
    probe.format_long_name = str(format_info.get("format_long_name") or "")
    format_tags = format_info.get("tags") or {}
    probe.major_brand = format_tags.get("major_brand")
    probe.compatible_brands = format_tags.get("compatible_brands")
    probe.muxer_encoder = format_tags.get("encoder")
    probe.production_provenance = _production_provenance(media_path)
    probe.base_clip_identity = probe.production_provenance.get("base_clip_identity")
    try:
        probe.duration_seconds = float(format_info.get("duration"))
    except (TypeError, ValueError):
        probe.duration_seconds = None
    probe.faststart = _faststart(media_path)
    probe.chapters = len(payload.get("chapters") or [])

    streams = payload.get("streams") or []
    videos = [s for s in streams if s.get("codec_type") == "video"]
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    probe.video_stream_count = len(videos)
    probe.audio_stream_count = len(audios)
    probe.subtitle_stream_count = sum(s.get("codec_type") == "subtitle" for s in streams)
    probe.data_stream_count = sum(s.get("codec_type") == "data" for s in streams)
    probe.attached_pictures = sum(
        bool((s.get("disposition") or {}).get("attached_pic")) for s in videos
    )

    primary_videos = [
        s for s in videos if not bool((s.get("disposition") or {}).get("attached_pic"))
    ]
    if len(primary_videos) > 1:
        default_videos = [
            s for s in primary_videos if bool((s.get("disposition") or {}).get("default"))
        ]
        if len(default_videos) == 1:
            primary_videos = default_videos + [
                s for s in primary_videos if s is not default_videos[0]
            ]
        else:
            probe.primary_stream_ambiguous = True
    if primary_videos:
        video = primary_videos[0]
        probe.primary_video_stream_index = video.get("index")
        probe.video_codec = video.get("codec_name")
        probe.video_encoder = (video.get("tags") or {}).get("encoder")
        probe.video_profile = video.get("profile")
        probe.h264_level = video.get("level")
        probe.has_b_frames = video.get("has_b_frames")
        probe.pixel_format = video.get("pix_fmt")
        probe.width = video.get("width")
        probe.height = video.get("height")
        probe.sample_aspect_ratio = video.get("sample_aspect_ratio")
        probe.field_order = video.get("field_order")
        probe.r_frame_rate = video.get("r_frame_rate")
        probe.avg_frame_rate = video.get("avg_frame_rate")
        nominal = _fraction(probe.r_frame_rate)
        average = _fraction(probe.avg_frame_rate)
        if nominal and average:
            probe.source_frame_rate_mode = (
                "cfr" if abs(float(nominal - average)) <= 0.01 else "vfr"
            )
        probe.rotation = _rotation(video)
        probe.color_range = video.get("color_range")
        probe.color_space = video.get("color_space")
        probe.color_primaries = video.get("color_primaries")
        probe.color_transfer = video.get("color_transfer")
        probe.chroma_location = video.get("chroma_location")
        try:
            probe.bits_per_raw_sample = int(video.get("bits_per_raw_sample"))
        except (TypeError, ValueError):
            probe.bits_per_raw_sample = None
        probe.hdr = _is_hdr(video)
        probe.color_conflict = _color_conflict(video)
        if probe.color_conflict:
            probe.codec_vui = _codec_vui(media_path)
            decoded_color, frame_side_data = _decoded_frame_metadata(media_path)
            probe.decoded_frame_color = decoded_color
            probe.hdr_side_data = _dedupe_side_data(
                list(video.get("side_data_list") or []) + frame_side_data
            )
            probe.mastering_display_metadata = [
                item for item in probe.hdr_side_data
                if "mastering display" in str(item.get("side_data_type") or "").casefold()
            ]
            probe.content_light_metadata = [
                item for item in probe.hdr_side_data
                if "content light" in str(item.get("side_data_type") or "").casefold()
            ]
            probe.source_sha256 = _sha256_file(media_path)
            probe.color_policy_override = resolve_approved_color_override(probe)
        if probe.duration_seconds is None:
            try:
                probe.duration_seconds = float(video.get("duration"))
            except (TypeError, ValueError):
                pass
    if audios:
        selected_audios = list(audios)
        if len(audios) > 1:
            default_audios = [
                s for s in audios if bool((s.get("disposition") or {}).get("default"))
            ]
            if len(default_audios) == 1:
                selected_audios = default_audios + [
                    s for s in audios if s is not default_audios[0]
                ]
            else:
                probe.primary_stream_ambiguous = True
        audio = selected_audios[0]
        probe.primary_audio_stream_index = audio.get("index")
        probe.audio_codec = audio.get("codec_name")
        probe.audio_profile = audio.get("profile")
        probe.audio_channels = audio.get("channels")
        probe.audio_channel_layout = audio.get("channel_layout")
        try:
            probe.audio_sample_rate = int(audio.get("sample_rate"))
        except (TypeError, ValueError):
            probe.audio_sample_rate = None
    return probe


def full_decode(path: str | Path, *, timeout: int = 1_800) -> tuple[bool, str | None]:
    null_target = "NUL" if os.name == "nt" else "/dev/null"
    command = [
        "ffmpeg",
        "-v",
        "error",
        "-xerror",
        "-i",
        str(path),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-f",
        "null",
        null_target,
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    error = (completed.stderr or "").strip()
    return completed.returncode == 0, error or None


def _container_is_mp4(probe: MediaProbe) -> bool:
    names = {part.strip() for part in probe.format_name.split(",")}
    return bool(names & {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"})


def _aac_lc(profile: str | None) -> bool:
    return str(profile or "").strip().casefold() in {"lc", "aac lc", "aac-lc"}


def classify_media(probe: MediaProbe, policy: MediaPolicy) -> ClassificationResult:
    if probe.probe_error:
        return ClassificationResult(
            Classification.UNSUPPORTED, ("probe_failed",), probe
        )
    if probe.video_stream_count < 1:
        return ClassificationResult(
            Classification.UNSUPPORTED, ("missing_video_stream",), probe
        )
    if probe.duration_seconds is None or probe.duration_seconds <= 0:
        return ClassificationResult(
            Classification.UNSUPPORTED, ("invalid_duration",), probe
        )
    if probe.color_conflict and not probe.color_policy_override:
        return ClassificationResult(
            Classification.UNSUPPORTED, ("color_metadata_conflict",), probe
        )
    if probe.primary_stream_ambiguous:
        return ClassificationResult(
            Classification.UNSUPPORTED, ("ambiguous_primary_stream",), probe
        )

    stream_reasons: list[str] = []
    if probe.color_policy_override:
        stream_reasons.append("color_metadata_override")
    if probe.size_bytes > policy.max_bytes:
        stream_reasons.append("oversized")
    if probe.video_codec != "h264":
        stream_reasons.append("video_codec")
    if str(probe.video_profile or "").casefold() not in {"main", "baseline"}:
        stream_reasons.append("h264_profile")
    if probe.has_b_frames != 0:
        stream_reasons.append("b_frames")
    if probe.pixel_format != "yuv420p":
        stream_reasons.append("pixel_format")
    if probe.h264_level and probe.h264_level > 41:
        stream_reasons.append("h264_level")
    if probe.field_order not in {None, "", "unknown", "progressive"}:
        stream_reasons.append("interlaced")
    if probe.rotation:
        stream_reasons.append("rotation")
    if probe.sample_aspect_ratio not in {None, "", "N/A", "1:1"}:
        stream_reasons.append("sample_aspect_ratio")
    if probe.source_frame_rate_mode != "cfr":
        stream_reasons.append("frame_rate_mode")
    final_fps = choose_final_fps(probe.r_frame_rate, probe.avg_frame_rate, max_fps=policy.max_fps)
    source_fps = _fraction(probe.avg_frame_rate) or _fraction(probe.r_frame_rate)
    if source_fps and source_fps != final_fps:
        stream_reasons.append("frame_rate")
    if probe.hdr:
        stream_reasons.append("hdr")
    if probe.color_range not in {"tv", "limited"}:
        stream_reasons.append("color_range")
    if probe.color_space not in {"bt709"}:
        stream_reasons.append("color_space")
    if probe.color_primaries not in {"bt709"}:
        stream_reasons.append("color_primaries")
    if probe.color_transfer not in {"bt709"}:
        stream_reasons.append("color_transfer")
    if probe.audio_stream_count >= 1:
        if probe.audio_codec != "aac":
            stream_reasons.append("audio_codec")
        if not _aac_lc(probe.audio_profile):
            stream_reasons.append("audio_profile")
        if probe.audio_channels not in {1, 2}:
            stream_reasons.append("audio_channels")
    if stream_reasons:
        return ClassificationResult(
            Classification.TRANSCODE, tuple(sorted(set(stream_reasons))), probe
        )

    container_reasons: list[str] = []
    if Path(probe.path).suffix.casefold() != ".mp4":
        container_reasons.append("mp4_extension")
    if not _container_is_mp4(probe):
        container_reasons.append("container")
    if str(probe.major_brand or "").strip() not in {
        "isom",
        "iso2",
        "iso4",
        "iso5",
        "iso6",
        "mp41",
        "mp42",
        "avc1",
        "M4V",
    }:
        container_reasons.append("mp4_brand")
    if not probe.faststart:
        container_reasons.append("faststart")
    if probe.chapters:
        container_reasons.append("chapters")
    if probe.subtitle_stream_count:
        container_reasons.append("subtitle_streams")
    if probe.data_stream_count:
        container_reasons.append("data_streams")
    if probe.attached_pictures:
        container_reasons.append("attached_picture")
    if probe.video_stream_count > 1 + probe.attached_pictures:
        container_reasons.append("extra_video_streams")
    if probe.audio_stream_count > 1:
        container_reasons.append("extra_audio_streams")
    if container_reasons:
        return ClassificationResult(
            Classification.REMUX, tuple(sorted(set(container_reasons))), probe
        )
    return ClassificationResult(Classification.COPY, (), probe)


def validate_delivery(
    path: str | Path,
    *,
    policy: MediaPolicy,
    action: ProcessingAction | str,
    expected_duration: float | None = None,
    require_target_size: bool = False,
    decode: bool = True,
) -> DeliveryComplianceResult:
    probe = probe_media(path)
    classified = classify_media(probe, policy)
    failures = list(classified.reasons)
    if classified.classification is not Classification.COPY:
        failures.append(f"classification_{classified.classification.value}")
    if require_target_size and probe.size_bytes > policy.target_bytes:
        failures.append("encoded_output_above_target")
    if expected_duration is not None and probe.duration_seconds is not None:
        tolerance = max(0.15, min(0.5, expected_duration * 0.01))
        if abs(probe.duration_seconds - expected_duration) > tolerance:
            failures.append("duration_mismatch")
    decode_error = None
    if decode and not probe.probe_error:
        probe.full_decode_passed, decode_error = full_decode(path)
        if not probe.full_decode_passed:
            failures.append("full_decode_failed")
    diagnostics = probe.to_dict()
    diagnostics.update(
        {
            "processing_action": str(
                action.value if isinstance(action, ProcessingAction) else action
            ),
            "policy_revision": policy.revision,
            "final_size_bytes": probe.size_bytes,
            "final_frame_rate": str(
                choose_final_fps(probe.r_frame_rate, probe.avg_frame_rate)
            ),
            "final_color_range": probe.color_range,
            "final_color_space": probe.color_space,
            "final_color_primaries": probe.color_primaries,
            "final_color_transfer": probe.color_transfer,
            "decode_error": decode_error,
        }
    )
    unique_failures = sorted(set(failures))
    return DeliveryComplianceResult(
        compliant=not unique_failures,
        policy_revision=policy.revision,
        action=str(action.value if isinstance(action, ProcessingAction) else action),
        diagnostics=diagnostics,
        failure_codes=unique_failures,
    )


def remux_media(source: str | Path, destination: str | Path) -> subprocess.CompletedProcess[str]:
    Path(destination).parent.mkdir(parents=True, exist_ok=True)
    probe = probe_media(source)
    video_map = (
        f"0:{probe.primary_video_stream_index}"
        if probe.primary_video_stream_index is not None
        else "0:v:0"
    )
    audio_map = (
        f"0:{probe.primary_audio_stream_index}?"
        if probe.primary_audio_stream_index is not None
        else "0:a:0?"
    )
    command = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-noautorotate",
        "-i",
        str(source),
        "-map",
        video_map,
        "-map",
        audio_map,
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-sn",
        "-dn",
        str(destination),
    ]
    return subprocess.run(command, capture_output=True, text=True, check=False)


def _delivery_dimensions(
    width: int | None, height: int | None, target_bps: int, policy: MediaPolicy
) -> tuple[int, int]:
    vertical = (height or 0) >= (width or 0)
    cap_w, cap_h = (
        (720, 1280)
        if target_bps < policy.threshold_720p_bps
        else (1080, 1920)
    )
    if not vertical:
        cap_w, cap_h = cap_h, cap_w
    source_w, source_h = max(2, width or cap_w), max(2, height or cap_h)
    scale = min(1.0, cap_w / source_w, cap_h / source_h)
    return (
        max(2, int(source_w * scale) // 2 * 2),
        max(2, int(source_h * scale) // 2 * 2),
    )


def _transcode_filter(probe: MediaProbe, width: int, height: int, fps: Fraction) -> str:
    filters: list[str] = []
    if probe.rotation in {90, 270}:
        filters.append("transpose=1" if probe.rotation == 90 else "transpose=2")
    elif probe.rotation == 180:
        filters.extend(("hflip", "vflip"))
    filters.extend(source_color_normalization_filters(probe))
    filters.extend(
        (
            f"scale={width}:{height}:flags=lanczos",
            f"fps={fps.numerator}/{fps.denominator}",
            "setsar=1",
            "format=yuv420p",
        )
    )
    return ",".join(filters)


def source_color_normalization_filters(probe: MediaProbe) -> list[str]:
    """Return an explicit source-to-limited-BT.709 conversion path."""
    if probe.color_policy_override:
        override = probe.color_policy_override
        return [
            "zscale="
            f"rangein={override['input_range']}:"
            f"matrixin={override['input_matrix']}:"
            f"transferin={override['input_transfer']}:"
            f"primariesin={override['input_primaries']}:"
            "range=limited:matrix=bt709:transfer=bt709:primaries=bt709"
        ]
    input_range = "full" if probe.color_range in {"pc", "jpeg"} or probe.pixel_format == "yuvj420p" else "limited"
    input_matrix = probe.color_space or ("bt709" if max(probe.width or 0, probe.height or 0) >= 720 else "smpte170m")
    input_primaries = probe.color_primaries or (
        "bt709" if max(probe.width or 0, probe.height or 0) >= 720 else "smpte170m"
    )
    input_transfer = probe.color_transfer or (
        "bt709" if max(probe.width or 0, probe.height or 0) >= 720 else "smpte170m"
    )
    if probe.hdr:
        transfer_in = probe.color_transfer or "arib-std-b67"
        primaries_in = probe.color_primaries or "bt2020"
        matrix_in = probe.color_space or "bt2020nc"
        filters = [
            "zscale="
            f"rangein={input_range}:matrixin={matrix_in}:"
            f"transferin={transfer_in}:primariesin={primaries_in}:"
            "transfer=linear:npl=100"
        ]
        filters.append("format=gbrpf32le")
        filters.append("tonemap=tonemap=hable:desat=0")
        filters.append(
            "zscale=range=limited:matrix=bt709:transfer=bt709:primaries=bt709"
        )
    else:
        filters = [
            "zscale="
            f"rangein={input_range}:matrixin={input_matrix}:"
            f"transferin={input_transfer}:primariesin={input_primaries}:"
            "range=limited:matrix=bt709:transfer=bt709:primaries=bt709"
        ]
    return filters


def build_transcode_command(
    source: str | Path,
    destination: str | Path,
    *,
    probe: MediaProbe,
    policy: MediaPolicy,
    target_video_bps: int | None = None,
    encoder: str = "h264_nvenc",
) -> tuple[list[str], BitratePlan, Fraction, tuple[int, int]]:
    plan = calculate_bitrate_plan(
        probe.duration_seconds or 0.25,
        has_audio=probe.audio_stream_count > 0,
        policy=policy,
    )
    if target_video_bps is not None:
        plan = BitratePlan(
            **{
                **asdict(plan),
                "target_video_bps": int(target_video_bps),
                "maxrate_bps": max(int(target_video_bps), int(target_video_bps * 1.15)),
                "bufsize_bps": max(int(target_video_bps), int(target_video_bps * 2)),
            }
        )
    fps = choose_final_fps(
        probe.r_frame_rate, probe.avg_frame_rate, max_fps=policy.max_fps
    )
    width, height = _delivery_dimensions(
        probe.width, probe.height, plan.target_video_bps, policy
    )
    level = "3.2" if width * height <= 720 * 1280 else "4.1"
    gop = max(1, round(float(fps) * policy.gop_seconds))
    video_map = (
        f"0:{probe.primary_video_stream_index}"
        if probe.primary_video_stream_index is not None
        else "0:v:0"
    )
    audio_map = (
        f"0:{probe.primary_audio_stream_index}?"
        if probe.primary_audio_stream_index is not None
        else "0:a:0?"
    )
    command = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-noautorotate",
        "-i",
        str(source),
        "-map",
        video_map,
        "-map",
        audio_map,
        "-sn",
        "-dn",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-vf",
        _transcode_filter(probe, width, height, fps),
        "-c:v",
        encoder,
    ]
    if encoder == "h264_nvenc":
        command.extend(
            [
                "-preset",
                policy.nvenc_preset,
                "-tune",
                policy.nvenc_tune,
                "-rc",
                "vbr",
                "-multipass",
                policy.nvenc_multipass,
                "-spatial-aq",
                "1",
                "-temporal-aq",
                "1",
                "-aq-strength",
                "8",
            ]
        )
    else:
        command.extend(["-preset", "slow"])
    command.extend(
        [
            "-profile:v",
            "main",
            "-level:v",
            level,
            "-bf",
            "0",
            "-b:v",
            str(plan.target_video_bps),
            "-maxrate",
            str(plan.maxrate_bps),
            "-bufsize",
            str(plan.bufsize_bps),
            "-r",
            f"{fps.numerator}/{fps.denominator}",
            "-fps_mode",
            "cfr",
            "-g",
            str(gop),
            "-pix_fmt",
            "yuv420p",
            "-color_range",
            "tv",
            "-colorspace",
            "bt709",
            "-color_primaries",
            "bt709",
            "-color_trc",
            "bt709",
            "-c:a",
            "aac",
            "-profile:a",
            "aac_low",
            "-b:a",
            str(policy.audio_bitrate_bps),
            "-ar",
            "48000",
            "-ac",
            "1" if probe.audio_channels == 1 else "2",
            "-movflags",
            "+faststart",
            str(destination),
        ]
    )
    return command, plan, fps, (width, height)


def transcode_media(
    source: str | Path,
    destination: str | Path,
    *,
    policy: MediaPolicy,
    target_video_bps: int | None = None,
) -> tuple[subprocess.CompletedProcess[str], BitratePlan]:
    probe = probe_media(source)
    command, plan, _fps, _dimensions = build_transcode_command(
        source,
        destination,
        probe=probe,
        policy=policy,
        target_video_bps=target_video_bps,
    )
    Path(destination).parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode and "nvenc" in (completed.stderr or "").casefold():
        command, plan, _fps, _dimensions = build_transcode_command(
            source,
            destination,
            probe=probe,
            policy=policy,
            target_video_bps=target_video_bps,
            encoder="libx264",
        )
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
    return completed, plan


def copy_media(source: str | Path, destination: str | Path) -> None:
    Path(destination).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def source_identity(path: str | Path, *, fingerprint_bytes: int = 64 * 1024) -> dict[str, Any]:
    source = Path(path)
    stat = source.stat()
    digest = hashlib.sha256()
    with source.open("rb") as handle:
        digest.update(handle.read(fingerprint_bytes))
        if stat.st_size > fingerprint_bytes:
            handle.seek(max(0, stat.st_size - fingerprint_bytes))
            digest.update(handle.read(fingerprint_bytes))
    return {
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "fast_fingerprint": digest.hexdigest(),
    }


def validate_ffmpeg_capabilities() -> tuple[bool, list[str]]:
    required = {
        "h264_nvenc": ("-profile:v main", "-bf 0"),
    }
    errors: list[str] = []
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        return False, ["ffmpeg_or_ffprobe_missing"]
    try:
        help_result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-h", "encoder=h264_nvenc"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, [str(exc)]
    text = f"{help_result.stdout}\n{help_result.stderr}".casefold()
    for token in ("p6", "main", "fullres", "spatial-aq", "temporal-aq"):
        if token not in text:
            errors.append(f"h264_nvenc_missing_{token}")
    return not errors, errors
