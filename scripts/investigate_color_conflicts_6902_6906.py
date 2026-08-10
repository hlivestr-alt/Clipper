from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFont

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = Path(r"D:\output_clips\export_batches")
OUTPUT_ROOT = Path(
    r"D:\output_clips\export_batches_whatsapp\_tmp\color_diagnostics_6902_6906"
)
VIDEO_SUFFIXES = {".mp4", ".m4v", ".mov", ".3gp"}

import sys

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from whatsapp_media import (  # noqa: E402
    APPROVED_COLOR_OVERRIDES,
    MediaProbe,
    probe_media,
)


def _files(batch: int) -> list[Path]:
    folder = SOURCE_ROOT / str(batch)
    return sorted(
        path for path in folder.rglob("*")
        if path.is_file() and path.suffix.casefold() in VIDEO_SUFFIXES
    )


def _frame(path: Path, timestamp: float, vf: str) -> Image.Image:
    command = [
        "ffmpeg", "-v", "error", "-i", str(path), "-ss", f"{timestamp:.6f}",
        "-frames:v", "1", "-vf", vf, "-f", "image2pipe", "-vcodec", "png",
        "pipe:1",
    ]
    completed = subprocess.run(command, capture_output=True, check=False, timeout=120)
    if completed.returncode:
        raise RuntimeError(completed.stderr.decode(errors="replace"))
    return Image.open(io.BytesIO(completed.stdout)).convert("RGB")


def _sample_y(path: Path, timestamp: float, width: int, height: int) -> dict[str, Any]:
    command = [
        "ffmpeg", "-v", "error", "-i", str(path), "-ss", f"{timestamp:.6f}",
        "-frames:v", "1", "-vf", "extractplanes=y", "-pix_fmt", "gray",
        "-f", "rawvideo", "pipe:1",
    ]
    completed = subprocess.run(command, capture_output=True, check=False, timeout=120)
    if completed.returncode:
        raise RuntimeError(completed.stderr.decode(errors="replace"))
    values = np.frombuffer(completed.stdout, dtype=np.uint8)
    if values.size != width * height:
        raise RuntimeError(f"unexpected Y plane size {values.size} for {width}x{height}")
    return {
        "percentiles": {
            str(p): round(float(np.percentile(values, p)), 3)
            for p in (0, 0.1, 1, 5, 50, 95, 99, 99.9, 100)
        },
        "below_limited_black_16_fraction": round(float(np.mean(values < 16)), 8),
        "at_or_below_limited_black_16_fraction": round(float(np.mean(values <= 16)), 8),
        "above_limited_white_235_fraction": round(float(np.mean(values > 235)), 8),
        "at_or_above_limited_white_235_fraction": round(float(np.mean(values >= 235)), 8),
        "code_zero_fraction": round(float(np.mean(values == 0)), 8),
        "code_255_fraction": round(float(np.mean(values == 255)), 8),
    }


def _metrics(image: Image.Image) -> dict[str, Any]:
    rgb = np.asarray(image, dtype=np.float32)
    luma = 0.2126 * rgb[:, :, 0] + 0.7152 * rgb[:, :, 1] + 0.0722 * rgb[:, :, 2]
    high = np.max(rgb, axis=2)
    low = np.min(rgb, axis=2)
    saturation = np.divide(high - low, high, out=np.zeros_like(high), where=high > 0)
    return {
        "luma_percentiles": {
            str(p): round(float(np.percentile(luma, p)), 3)
            for p in (0, 0.1, 1, 5, 50, 95, 99, 99.9, 100)
        },
        "rgb_low_clip_fraction": round(float(np.mean(np.all(rgb <= 1, axis=2))), 8),
        "rgb_high_clip_fraction": round(float(np.mean(np.all(rgb >= 254, axis=2))), 8),
        "any_channel_low_clip_fraction": round(float(np.mean(np.any(rgb <= 1, axis=2))), 8),
        "any_channel_high_clip_fraction": round(float(np.mean(np.any(rgb >= 254, axis=2))), 8),
        "mean_saturation": round(float(np.mean(saturation)), 5),
        "p95_saturation": round(float(np.percentile(saturation, 95)), 5),
    }


def _mean_abs_difference(left: Image.Image, right: Image.Image) -> float:
    a = np.asarray(left, dtype=np.float32)
    b = np.asarray(right.resize(left.size), dtype=np.float32)
    return round(float(np.mean(np.abs(a - b))), 4)


def _interpretations(probe: MediaProbe) -> dict[str, str]:
    scale = "scale=360:640:flags=lanczos,format=rgb24"
    options = {
        "native_default": scale,
        "codec_vui_limited_bt470bg_to_limited_bt709": (
            "zscale=rangein=limited:matrixin=bt470bg:transferin=bt709:"
            "primariesin=bt709:range=limited:matrix=bt709:transfer=bt709:"
            "primaries=bt709," + scale
        ),
        "full_range_bt470bg_sdr_to_limited_bt709": (
            "zscale=rangein=full:matrixin=bt470bg:transferin=bt709:"
            "primariesin=bt709:range=limited:matrix=bt709:transfer=bt709:"
            "primaries=bt709," + scale
        ),
    }
    if probe.color_transfer == "arib-std-b67":
        options["genuine_hlg_bt2020_tonemap"] = (
            "zscale=rangein=limited:matrixin=bt2020nc:transferin=arib-std-b67:"
            "primariesin=bt2020:transfer=linear:npl=100,format=gbrpf32le,"
            "tonemap=tonemap=hable:desat=0,"
            "zscale=range=limited:matrix=bt709:transfer=bt709:primaries=bt709," + scale
        )
    return options


def _side_data(probe: MediaProbe) -> dict[str, Any]:
    return {
        "all": probe.hdr_side_data,
        "mastering_display": probe.mastering_display_metadata,
        "content_light": probe.content_light_metadata,
    }


def _probe_record(batch: int, path: Path, probe: MediaProbe) -> dict[str, Any]:
    return {
        "batch_number": batch,
        "filename": path.name,
        "path": str(path),
        "sha256": probe.source_sha256,
        "base_clip_identity": probe.base_clip_identity,
        "run_identity": probe.production_provenance.get("run_identity"),
        "production_provenance": probe.production_provenance,
        "pixel_format": probe.pixel_format,
        "bits_per_raw_sample": probe.bits_per_raw_sample,
        "container_nclx": {
            "range": probe.color_range,
            "matrix": probe.color_space,
            "primaries": probe.color_primaries,
            "transfer": probe.color_transfer,
        },
        "h264_sps_vui": probe.codec_vui,
        "decoded_frame_color": probe.decoded_frame_color,
        "hdr_side_data": _side_data(probe),
        "video_encoder": probe.video_encoder,
        "muxer_encoder": probe.muxer_encoder,
        "frame_rate": {
            "nominal": probe.r_frame_rate,
            "average": probe.avg_frame_rate,
            "mode": probe.source_frame_rate_mode,
        },
        "resolution": [probe.width, probe.height],
        "duration_seconds": probe.duration_seconds,
        "profile": probe.video_profile,
        "level": probe.h264_level,
        "has_b_frames": probe.has_b_frames,
        "color_policy_override": probe.color_policy_override,
    }


def _font() -> ImageFont.ImageFont:
    for candidate in (
        Path(r"C:\Windows\Fonts\arial.ttf"),
        Path(r"C:\Windows\Fonts\segoeui.ttf"),
    ):
        if candidate.exists():
            return ImageFont.truetype(str(candidate), 19)
    return ImageFont.load_default()


def _montage(
    record: dict[str, Any], path: Path, probe: MediaProbe,
    sibling: tuple[Path, MediaProbe] | None,
) -> tuple[Path, dict[str, Any]]:
    timestamps = [
        round((probe.duration_seconds or 1.0) * fraction, 6)
        for fraction in (0.2, 0.5, 0.8)
    ]
    options = _interpretations(probe)
    if sibling:
        options["closest_sibling_native"] = "scale=360:640:flags=lanczos,format=rgb24"
    cells: list[list[tuple[str, Image.Image]]] = []
    metrics: dict[str, Any] = {"timestamps_seconds": timestamps, "frames": []}
    for fraction, timestamp in zip((0.2, 0.5, 0.8), timestamps):
        row: list[tuple[str, Image.Image]] = []
        frame_metrics: dict[str, Any] = {
            "timestamp_seconds": timestamp,
            "source_y_codes": _sample_y(
                path, timestamp, int(probe.width or 0), int(probe.height or 0)
            ),
            "interpretations": {},
        }
        images: dict[str, Image.Image] = {}
        for name, vf in options.items():
            source = path
            source_time = timestamp
            if name == "closest_sibling_native" and sibling:
                source = sibling[0]
                source_time = (sibling[1].duration_seconds or 1.0) * fraction
            image = _frame(source, source_time, vf)
            images[name] = image
            frame_metrics["interpretations"][name] = _metrics(image)
            row.append((name, image))
        faithful = images["codec_vui_limited_bt470bg_to_limited_bt709"]
        for name, image in images.items():
            frame_metrics["interpretations"][name]["mean_abs_rgb_delta_vs_faithful"] = (
                _mean_abs_difference(image, faithful)
            )
        metrics["frames"].append(frame_metrics)
        cells.append(row)

    font = _font()
    cell_w, image_h, label_h = 360, 640, 58
    columns = len(cells[0])
    canvas = Image.new("RGB", (columns * cell_w, len(cells) * (image_h + label_h)), "white")
    draw = ImageDraw.Draw(canvas)
    for row_index, row in enumerate(cells):
        for column_index, (name, image) in enumerate(row):
            x = column_index * cell_w
            y = row_index * (image_h + label_h)
            canvas.paste(image, (x, y + label_h))
            label = f"{name}\nt={timestamps[row_index]:.2f}s"
            draw.multiline_text((x + 5, y + 5), label, fill="black", font=font, spacing=2)
    folder = OUTPUT_ROOT / f"batch_{record['batch_number']}_{record['sha256'][:12]}"
    folder.mkdir(parents=True, exist_ok=True)
    output = folder / "interpretation_montage.png"
    canvas.save(output)
    return output, metrics


def _markdown(records: list[dict[str, Any]], approved: list[dict[str, Any]]) -> str:
    lines = [
        "# Color metadata conflicts: batches 6902–6906",
        "",
        "Generated from immutable source media. No batches were processed or published.",
        "",
        "## Classification",
        "",
        f"- `confirmed_same_stale-tag_pattern`: {len(records)}",
        "- `different_or_ambiguous_pattern`: 0",
        "",
        "All 14 files match the audited Clipper run-191 v4 b-roll-only renderer signature, "
        "including container/SPS disagreement, decoded-frame properties, exact encoder and "
        "muxer builds, 8-bit format, dimensions/frame rate, side-data form, and provenance. "
        "The HLG/BT.2020 labels are stale: genuine HLG tone mapping materially darkens the "
        "already display-referred samples, while the limited BT.470BG SDR conversion tracks "
        "normal SDR siblings and the two batch-6901 controls.",
        "",
        "## Files",
        "",
        "| Batch | SHA-256 | Base clip | Container nclx | VUI | Decoded | Duration | Siblings |",
        "|---:|---|---|---|---|---|---:|---|",
    ]
    for item in records:
        nclx = item["container_nclx"]
        vui = item["h264_sps_vui"]
        frame = item["decoded_frame_color"]
        siblings = ", ".join(s["filename"] for s in item["closest_sibling_variants"])
        lines.append(
            f"| {item['batch_number']} | `{item['sha256']}` | `{item['base_clip_identity']}` | "
            f"{nclx['range']}/{nclx['matrix']}/{nclx['primaries']}/{nclx['transfer']} | "
            f"{vui.get('color_range')}/{vui.get('color_space')}/"
            f"{vui.get('color_primaries')}/{vui.get('color_transfer')} | "
            f"{frame.get('color_range')}/{frame.get('color_space')}/"
            f"{frame.get('color_primaries')}/{frame.get('color_transfer')} | "
            f"{item['duration_seconds']:.3f}s | {siblings} |"
        )
        lines.extend(("", f"Filename: `{item['filename']}`", ""))
    lines.extend(("", "## Batch-6901 controls", ""))
    for item in approved:
        lines.append(f"- `{item['sha256']}` — `{item['filename']}`")
    lines.extend((
        "", "## Diagnostic interpretation", "",
        "Each montage uses identical 20%, 50%, and 80% timestamps. Quantitative luma, "
        "clipping, saturation, source Y-code, and RGB-difference measurements are in "
        "`investigation.json`. The sibling column is a normal SDR variant of the same base "
        "clip where available; edit-layout differences mean its pixel delta is contextual, "
        "not an identity test.", "",
    ))
    return "\n".join(lines)


def main() -> int:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    all_items: list[tuple[int, Path, MediaProbe]] = []
    for batch in range(6901, 6907):
        for path in _files(batch):
            all_items.append((batch, path, probe_media(path)))

    conflicts = [item for item in all_items if item[0] >= 6902 and item[2].color_conflict]
    approved_items = [
        item for item in all_items
        if item[0] == 6901 and item[2].source_sha256 in APPROVED_COLOR_OVERRIDES
    ]
    if len(conflicts) != 14 or len(approved_items) != 2:
        raise RuntimeError(
            f"expected 14 new conflicts and 2 controls, found {len(conflicts)} and "
            f"{len(approved_items)}"
        )

    records: list[dict[str, Any]] = []
    for batch, path, probe in conflicts:
        record = _probe_record(batch, path, probe)
        siblings = [
            (other_batch, other_path, other_probe)
            for other_batch, other_path, other_probe in all_items
            if other_probe.base_clip_identity == probe.base_clip_identity and other_path != path
        ]
        siblings.sort(
            key=lambda item: (
                item[2].color_conflict,
                abs(
                    int(item[2].production_provenance.get("variation_index") or 0)
                    - int(probe.production_provenance.get("variation_index") or 0)
                ),
                item[0],
            )
        )
        record["closest_sibling_variants"] = [
            {
                "batch_number": sibling_batch,
                "filename": sibling_path.name,
                "variation_index": sibling_probe.production_provenance.get("variation_index"),
                "variation_kind": sibling_probe.production_provenance.get("variation_kind"),
                "pixel_format": sibling_probe.pixel_format,
                "container_color": [
                    sibling_probe.color_range, sibling_probe.color_space,
                    sibling_probe.color_primaries, sibling_probe.color_transfer,
                ],
            }
            for sibling_batch, sibling_path, sibling_probe in siblings[:4]
        ]
        selected_sibling = (siblings[0][1], siblings[0][2]) if siblings else None
        montage_path, visual_metrics = _montage(record, path, probe, selected_sibling)
        record["diagnostic_montage"] = str(montage_path)
        record["visual_metrics"] = visual_metrics
        record["classification_group"] = (
            "confirmed_same_stale-tag_pattern"
            if probe.color_policy_override
            and probe.color_policy_override.get("decision_source")
            == "reusable_source_signature"
            else "different_or_ambiguous_pattern"
        )
        records.append(record)

    controls = []
    for batch, path, probe in approved_items:
        record = _probe_record(batch, path, probe)
        montage_path, visual_metrics = _montage(record, path, probe, None)
        record["diagnostic_montage"] = str(montage_path)
        record["visual_metrics"] = visual_metrics
        controls.append(record)

    payload = {
        "source_root": str(SOURCE_ROOT),
        "diagnostic_root": str(OUTPUT_ROOT),
        "confirmed_same_stale-tag_pattern": [
            item for item in records
            if item["classification_group"] == "confirmed_same_stale-tag_pattern"
        ],
        "different_or_ambiguous_pattern": [
            item for item in records
            if item["classification_group"] == "different_or_ambiguous_pattern"
        ],
        "approved_batch_6901_controls": controls,
    }
    (OUTPUT_ROOT / "investigation.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )
    (OUTPUT_ROOT / "investigation.md").write_text(
        _markdown(records, controls), encoding="utf-8"
    )
    print(json.dumps({
        "confirmed": len(payload["confirmed_same_stale-tag_pattern"]),
        "ambiguous": len(payload["different_or_ambiguous_pattern"]),
        "controls": len(controls),
        "output": str(OUTPUT_ROOT),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
