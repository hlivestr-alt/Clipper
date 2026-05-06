#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path


ASSET_COLUMNS = [
    "asset_id",
    "base_clip_id",
    "source_video",
    "run_tag",
    "clip_id",
    "variant_id",
    "variant_name",
    "score",
    "hook",
    "product",
    "clip_type",
    "start",
    "end",
    "duration",
    "status",
    "assigned_to",
    "assigned_at",
    "uploaded_at",
    "file_path",
    "file_name",
    "file_size_bytes",
    "modified_at",
    "manifest_status",
    "manifest_path",
    "imported_at",
]


CLIP_RE = re.compile(
    r"^(?P<base>clip_\d+)"
    r"(?:_v(?P<variant_num>\d+)_(?P<variant_name>.+?))?"
    r"_score(?P<score>\d+(?:\.\d+)?)"
    r"_(?P<hook>.+)\.mp4$",
    re.IGNORECASE,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Import existing rendered clips into assets.csv")
    parser.add_argument("--clips-dir", default=r"D:\output_clips", help="Root folder containing output clip folders")
    parser.add_argument("--output", default="assets.csv", help="CSV file to write")
    parser.add_argument("--include-failed", action="store_true", help="Include manifest rows marked failed")
    parser.add_argument("--min-size-bytes", type=int, default=1024, help="Skip tiny/broken MP4 files below this size")
    args = parser.parse_args()

    clips_dir = Path(args.clips_dir)
    if not clips_dir.exists():
        raise FileNotFoundError(f"Clips folder not found: {clips_dir}")

    imported_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    rows = []

    for folder in sorted([p for p in clips_dir.iterdir() if p.is_dir()], key=lambda p: p.name.casefold()):
        source_video, run_tag = split_output_folder_name(folder.name)
        manifest_path = folder / "manifest.json"
        manifest_by_file = load_manifest_by_file(manifest_path)

        for mp4 in sorted(folder.rglob("*.mp4"), key=lambda p: p.relative_to(folder).as_posix().casefold()):
            if mp4.stat().st_size < args.min_size_bytes:
                continue

            relative_file = mp4.relative_to(folder).as_posix()
            manifest_row = manifest_by_file.get(relative_file) or manifest_by_file.get(mp4.name, {})
            manifest_status = str(manifest_row.get("status", "") or "")
            if manifest_status == "failed" and not args.include_failed:
                continue

            parsed = parse_clip_filename(mp4.name)
            clip_id = str(manifest_row.get("clip_id") or parsed["clip_id"])
            base_clip_id = strip_variant_suffix(clip_id)
            variant_id, variant_name = variant_from_clip_id(clip_id, parsed)
            score = manifest_row.get("score", parsed["score"])
            hook = manifest_row.get("hook") or parsed["hook"]
            file_path = str(mp4.resolve())

            rows.append(
                {
                    "asset_id": build_asset_id(source_video, run_tag, clip_id),
                    "base_clip_id": f"{source_video}::{base_clip_id}",
                    "source_video": source_video,
                    "run_tag": run_tag,
                    "clip_id": clip_id,
                    "variant_id": variant_id,
                    "variant_name": variant_name,
                    "score": score,
                    "hook": hook,
                    "product": manifest_row.get("product", ""),
                    "clip_type": manifest_row.get("clip_type", ""),
                    "start": manifest_row.get("start", ""),
                    "end": manifest_row.get("end", ""),
                    "duration": manifest_row.get("duration", ""),
                    "status": "available",
                    "assigned_to": "",
                    "assigned_at": "",
                    "uploaded_at": "",
                    "file_path": file_path,
                    "file_name": mp4.name,
                    "file_size_bytes": mp4.stat().st_size,
                    "modified_at": datetime.fromtimestamp(mp4.stat().st_mtime).astimezone().isoformat(timespec="seconds"),
                    "manifest_status": manifest_status,
                    "manifest_path": str(manifest_path.resolve()) if manifest_path.exists() else "",
                    "imported_at": imported_at,
                }
            )
    rows.sort(key=lambda row: (row["run_tag"], row["source_video"], row["base_clip_id"], row["variant_id"], row["file_name"]))
    write_csv(Path(args.output), rows)
    print(f"Imported {len(rows)} asset(s) from {clips_dir} -> {args.output}")
    return 0


def split_output_folder_name(folder_name: str) -> tuple[str, str]:
    if "__" not in folder_name:
        return folder_name, ""
    source_video, run_tag = folder_name.rsplit("__", 1)
    return source_video, run_tag


def load_manifest_by_file(manifest_path: Path) -> dict[str, dict]:
    if not manifest_path.exists():
        return {}
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        return {}
    if not isinstance(payload, list):
        return {}
    by_file = {}
    for row in payload:
        if not isinstance(row, dict) or not row.get("output_file"):
            continue
        output_file = str(row.get("output_file"))
        normalized = output_file.replace("\\", "/")
        by_file[normalized] = row
        by_file[Path(normalized).name] = row
    return by_file


def parse_clip_filename(file_name: str) -> dict[str, str]:
    match = CLIP_RE.match(file_name)
    if not match:
        stem = Path(file_name).stem
        return {
            "clip_id": stem,
            "base_clip_id": stem,
            "variant_id": "",
            "variant_name": "",
            "score": "",
            "hook": "",
        }

    base = match.group("base")
    variant_num = match.group("variant_num")
    variant_name = match.group("variant_name") or ""
    clip_id = f"{base}_v{variant_num}_{variant_name}" if variant_num is not None else base
    return {
        "clip_id": clip_id,
        "base_clip_id": base,
        "variant_id": f"v{variant_num}" if variant_num is not None else "",
        "variant_name": variant_name,
        "score": match.group("score") or "",
        "hook": (match.group("hook") or "").replace("_", " "),
    }


def strip_variant_suffix(clip_id: str) -> str:
    match = re.match(r"^(clip_\d+)(?:_v\d+_.+)?$", clip_id)
    return match.group(1) if match else clip_id


def variant_from_clip_id(clip_id: str, parsed: dict[str, str]) -> tuple[str, str]:
    match = re.match(r"^clip_\d+_(v\d+)_(.+)$", clip_id)
    if match:
        return match.group(1), match.group(2)
    return parsed.get("variant_id", ""), parsed.get("variant_name", "")


def build_asset_id(source_video: str, run_tag: str, clip_id: str) -> str:
    raw = f"{source_video}::{run_tag}::{clip_id}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
    return f"{source_video}__{run_tag}__{clip_id}__{digest}"


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True) if path.parent != Path("") else None
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=ASSET_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    raise SystemExit(main())
