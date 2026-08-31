from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from array import array
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clipper_app.modular_renderer.media import FFmpegPipeline, probe_media


def decoded_payload(path: Path, mapping: str, output_format: str) -> bytes:
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path), "-map", mapping]
    if output_format == "video":
        command.extend(["-f", "framemd5", "-"])
    else:
        command.extend(["-f", "s16le", "-ar", "48000", "-ac", "2", "-"])
    completed = subprocess.run(command, capture_output=True, check=True, timeout=300)
    return completed.stdout


def audio_alignment(left: bytes, right: bytes) -> tuple[float, int]:
    a = array("h", left)[::2][:480000]
    b = array("h", right)[::2][:480000]
    scores = []
    for shift in range(-2, 3):
        aa = a[max(0, shift):min(len(a), len(a) + shift)]
        bb = b[max(0, -shift):min(len(b), len(b) - shift)]
        dot = sum(x * y for x, y in zip(aa, bb))
        norm_a = sum(x * x for x in aa) ** 0.5
        norm_b = sum(y * y for y in bb) ** 0.5
        scores.append((dot / (norm_a * norm_b + 1e-12), shift))
    return max(scores)


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare renderer v1 output-seek with v1.1 input accurate-seek.")
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--starts", required=True, nargs="+", type=float)
    parser.add_argument("--duration", type=float, default=20.0)
    parser.add_argument("--output-dir", type=Path, default=Path("working/modular_renderer_seek_benchmark"))
    parser.add_argument("--reuse-old", action="store_true", help="Reuse existing old outputs and timings from benchmark.json")
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    source_info = probe_media(args.source)
    pipeline = FFmpegPipeline()
    width = int(source_info["width"]) // 2 * 2
    height = int(source_info["height"]) // 2 * 2
    previous_rows = []
    report_path = args.output_dir / "benchmark.json"
    if args.reuse_old and report_path.exists():
        previous_rows = json.loads(report_path.read_text(encoding="utf-8")).get("rows", [])
    rows = []
    for index, start in enumerate(args.starts):
        outputs = {}
        timings = {}
        for label, fast in (("old", False), ("new", True)):
            output = args.output_dir / f"range_{index:02d}_{label}.mp4"
            if label == "old" and args.reuse_old and output.exists() and index < len(previous_rows):
                timings[label] = float(previous_rows[index]["old_seconds"])
                outputs[label] = output
                continue
            command = pipeline._extract_command(
                args.source, output, start, args.duration, width, height, fast=fast,
            )
            began = time.perf_counter()
            pipeline._run(command, timeout=max(300.0, args.duration * 10.0))
            timings[label] = time.perf_counter() - began
            pipeline.validate_segment(output, args.duration)
            outputs[label] = output
        old_info, new_info = probe_media(outputs["old"]), probe_media(outputs["new"])
        old_video = decoded_payload(outputs["old"], "0:v:0", "video")
        new_video = decoded_payload(outputs["new"], "0:v:0", "video")
        old_audio = decoded_payload(outputs["old"], "0:a:0", "audio")
        new_audio = decoded_payload(outputs["new"], "0:a:0", "audio")
        audio_correlation, audio_shift = audio_alignment(old_audio, new_audio)
        row = {
            "source_filename": args.source.name,
            "source_duration": source_info["duration"],
            "start": start,
            "duration": args.duration,
            "old_seconds": timings["old"],
            "new_seconds": timings["new"],
            "speedup": timings["old"] / timings["new"],
            "old_output_duration": old_info["duration"],
            "new_output_duration": new_info["duration"],
            "decoded_video_equal": hashlib.sha256(old_video).digest() == hashlib.sha256(new_video).digest(),
            "decoded_audio_equal": hashlib.sha256(old_audio).digest() == hashlib.sha256(new_audio).digest(),
            "audio_best_shift_samples": audio_shift,
            "audio_correlation": audio_correlation,
        }
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    report = {
        "source_filename": args.source.name,
        "source_duration": source_info["duration"],
        "rows": rows,
    }
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
