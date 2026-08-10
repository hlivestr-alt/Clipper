"""Audit blocked stale-container-nclx candidates without changing source media.

The script deliberately runs one FFmpeg process at a time.  It writes only
diagnostic JSON/PNG files under the caller-provided destination directory.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
from pathlib import Path

import numpy as np
from PIL import Image


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=r"D:\output_clips\export_batches")
    parser.add_argument(
        "--db", default=r"D:\output_clips\export_batches_whatsapp\_whatsapp_state.sqlite3"
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--representatives",
        default=r"D:\output_clips\export_batches_whatsapp\_tmp\color_diagnostics_remaining\representatives.json",
    )
    parser.add_argument(
        "--output",
        default=r"D:\output_clips\export_batches_whatsapp\_tmp\color_diagnostics_remaining",
    )
    parser.add_argument("--skip-full-decode", action="store_true")
    return parser.parse_args()


def _run(command: list[str], *, timeout: int = 1800) -> tuple[bool, str, float, bytes]:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc), time.monotonic() - started, b""
    error = (completed.stderr or b"").decode(errors="replace").strip()
    return completed.returncode == 0, error, time.monotonic() - started, completed.stdout


def _stats(raw: bytes, width: int, height: int) -> dict[str, object]:
    if not raw:
        return {"error": "empty_frame"}
    pixels = np.frombuffer(raw, dtype=np.uint8)
    expected = width * height * 3
    if pixels.size != expected:
        return {"error": f"unexpected_rgb_bytes:{pixels.size}!={expected}"}
    rgb = pixels.reshape((height, width, 3)).astype(np.float32)
    luma = 0.2126 * rgb[:, :, 0] + 0.7152 * rgb[:, :, 1] + 0.0722 * rgb[:, :, 2]
    vmax = rgb.max(axis=2)
    vmin = rgb.min(axis=2)
    saturation = np.divide(
        vmax - vmin,
        np.maximum(vmax, 1.0),
        out=np.zeros_like(vmax),
        where=vmax > 0,
    )
    percentiles = np.percentile(luma, [0, 1, 5, 50, 95, 99, 100])
    return {
        "luma_percentiles": {
            key: round(float(value), 3)
            for key, value in zip(("p0", "p1", "p5", "p50", "p95", "p99", "p100"), percentiles)
        },
        "luma_range_occupancy_16_235": round(float(np.mean((luma >= 16) & (luma <= 235))), 6),
        "luma_near_black_le_20": round(float(np.mean(luma <= 20)), 6),
        "luma_near_white_ge_230": round(float(np.mean(luma >= 230)), 6),
        "saturation_mean": round(float(saturation.mean()), 6),
        "saturation_p95": round(float(np.percentile(saturation, 95)), 6),
        "rgb_mean": [round(float(x), 3) for x in rgb.mean(axis=(0, 1))],
    }


def _variant_filter(name: str) -> str | None:
    if name == "native":
        return None
    if name == "stale_sdr":
        return (
            "zscale=rangein=limited:matrixin=bt470bg:transferin=bt709:"
            "primariesin=bt709:range=limited:matrix=bt709:transfer=bt709:primaries=bt709"
        )
    if name == "full_range_sdr":
        return (
            "zscale=rangein=full:matrixin=bt709:transferin=bt709:primariesin=bt709:"
            "range=limited:matrix=bt709:transfer=bt709:primaries=bt709"
        )
    if name == "hlg_tonemap":
        return (
            "zscale=rangein=limited:matrixin=bt470bg:transferin=arib-std-b67:"
            "primariesin=bt2020:transfer=linear:npl=100,format=gbrpf32le,"
            "tonemap=tonemap=hable:desat=0,"
            "zscale=range=limited:matrix=bt709:transfer=bt709:primaries=bt709"
        )
    raise ValueError(name)


def _extract_variant(source: Path, out_png: Path, name: str) -> dict[str, object]:
    frame_width, frame_height = 360, 640
    filt = _variant_filter(name)
    filters = []
    if filt:
        filters.append(filt)
    filters.append(f"scale={frame_width}:{frame_height}:force_original_aspect_ratio=decrease")
    filters.append("pad=360:640:(ow-iw)/2:(oh-ih)/2")
    filters.append("format=rgb24")
    command = [
        "ffmpeg",
        "-v",
        "error",
        "-xerror",
        "-ss",
        "0.5",
        "-i",
        str(source),
        "-frames:v",
        "1",
        "-vf",
        ",".join(filters),
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "pipe:1",
    ]
    ok, error, elapsed, raw = _run(command, timeout=300)
    result: dict[str, object] = {"ok": ok, "elapsed_seconds": round(elapsed, 3)}
    if not ok:
        result["error"] = error
        return result
    try:
        image = Image.frombytes("RGB", (frame_width, frame_height), raw)
        image.save(out_png)
        result["path"] = str(out_png)
        result["stats"] = _stats(raw, frame_width, frame_height)
    except Exception as exc:  # pragma: no cover - diagnostic safeguard
        result["ok"] = False
        result["error"] = str(exc)
    return result


def _full_decode(source: Path) -> dict[str, object]:
    null_target = "NUL" if shutil.which("ffmpeg") and Path("NUL").anchor else "NUL"
    command = [
        "ffmpeg",
        "-v",
        "error",
        "-xerror",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-f",
        "null",
        null_target,
    ]
    ok, error, elapsed, _raw = _run(command, timeout=1800)
    payload: dict[str, object] = {"ok": ok, "elapsed_seconds": round(elapsed, 3)}
    if not ok:
        payload["error"] = error
    return payload


def main() -> int:
    args = _parse_args()
    source_root = Path(args.source)
    output_root = Path(args.output)
    output_root.mkdir(parents=True, exist_ok=True)
    import sqlite3

    connection = sqlite3.connect(args.db)
    connection.row_factory = sqlite3.Row
    blocked = connection.execute(
        """
        SELECT batch_number,relative_path,probe_json
        FROM media_processing_files
        WHERE run_id=? AND status='failed'
        ORDER BY batch_number,relative_path
        """,
        (args.run_id,),
    ).fetchall()
    connection.close()

    decode_path = output_root / "source_full_decode.json"
    if args.skip_full_decode and decode_path.exists():
        full_decode = json.loads(decode_path.read_text(encoding="utf-8"))
    else:
        full_decode = {"count": len(blocked), "passed": 0, "failed": 0, "errors": []}
        for index, row in enumerate(blocked, 1):
            path = source_root / str(row["batch_number"]) / str(row["relative_path"])
            result = _full_decode(path)
            if result["ok"]:
                full_decode["passed"] += 1
            else:
                full_decode["failed"] += 1
                full_decode["errors"].append(
                    {"batch": int(row["batch_number"]), "relative_path": str(row["relative_path"]), **result}
                )
            if index % 10 == 0:
                decode_path.write_text(json.dumps(full_decode, indent=2), encoding="utf-8")
        decode_path.write_text(json.dumps(full_decode, indent=2), encoding="utf-8")

    representatives = json.loads(Path(args.representatives).read_text(encoding="utf-8"))
    representative_results: list[dict[str, object]] = []
    for item in representatives:
        representative = item["representative"]
        batch = int(representative["batch"])
        relative = str(representative["relative_path"])
        source = source_root / str(batch) / relative
        cohort_root = output_root / str(item["evidence_cohort_id"])
        cohort_root.mkdir(parents=True, exist_ok=True)
        variants = ["native", "stale_sdr", "full_range_sdr"]
        if representative["probe"].get("hdr"):
            variants.append("hlg_tonemap")
        result = {
            "evidence_cohort_id": item["evidence_cohort_id"],
            "count": item["count"],
            "batch": batch,
            "relative_path": relative,
            "source": str(source),
            "variants": {},
        }
        images: list[Image.Image] = []
        labels: list[str] = []
        for variant in variants:
            png = cohort_root / f"{variant}.png"
            variant_result = _extract_variant(source, png, variant)
            result["variants"][variant] = variant_result
            if variant_result.get("ok"):
                try:
                    images.append(Image.open(png).convert("RGB"))
                    labels.append(variant)
                except OSError:
                    pass
        if images:
            sheet = Image.new("RGB", (360 * len(images), 680), "white")
            for idx, image in enumerate(images):
                sheet.paste(image, (idx * 360, 40))
            sheet.save(cohort_root / "comparison.png")
            result["comparison_path"] = str(cohort_root / "comparison.png")
            result["variant_order"] = labels
        representative_results.append(result)

    payload = {
        "run_id": args.run_id,
        "blocked_count": len(blocked),
        "full_decode": full_decode,
        "representatives": representative_results,
    }
    (output_root / "diagnostics.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps({"blocked": len(blocked), "full_decode": full_decode, "representatives": len(representative_results), "output": str(output_root / 'diagnostics.json')}, indent=2))
    return 0 if not full_decode["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
