from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from .constants import VIDEO_SUFFIXES


def _fingerprint(path: Path, block_size: int = 1024 * 1024) -> str:
    stat = path.stat()
    digest = hashlib.sha256()
    digest.update(str(stat.st_size).encode("ascii"))
    with path.open("rb") as handle:
        digest.update(handle.read(block_size))
        if stat.st_size > block_size:
            handle.seek(max(0, stat.st_size - block_size))
            digest.update(handle.read(block_size))
    return digest.hexdigest()


def probe_duration(path: Path) -> float | None:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30, check=True,
        )
        value = float(json.loads(result.stdout)["format"]["duration"])
        return value if value > 0 else None
    except (OSError, subprocess.SubprocessError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def source_record(path: str | Path, *, include_duration: bool = True) -> dict[str, Any]:
    resolved = Path(path).resolve(strict=True)
    if not resolved.is_file() or resolved.suffix.casefold() not in VIDEO_SUFFIXES:
        raise ValueError("Unsupported VOD source")
    stat = resolved.stat()
    content = _fingerprint(resolved)
    identity = hashlib.sha256(
        f"{str(resolved).casefold()}|{stat.st_size}|{stat.st_mtime_ns}|{content}".encode("utf-8")
    ).hexdigest()[:32]
    return {
        "source_id": identity,
        "filename": resolved.name,
        "canonical_path": str(resolved),
        "file_size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "content_fingerprint": content,
        "duration_seconds": probe_duration(resolved) if include_duration else None,
    }


def discover_sources(input_dir: str | Path, *, include_duration: bool = True) -> list[dict[str, Any]]:
    root = Path(input_dir).resolve()
    if not root.is_dir():
        return []
    sources = []
    for path in sorted(root.iterdir(), key=lambda item: item.name.casefold()):
        if path.is_file() and path.suffix.casefold() in VIDEO_SUFFIXES:
            try:
                sources.append(source_record(path, include_duration=include_duration))
            except OSError:
                continue
    return sources


def revalidate_source(source: dict[str, Any], input_dir: str | Path) -> Path:
    root = Path(input_dir).resolve(strict=True)
    candidate = Path(source["canonical_path"]).resolve(strict=True)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise PermissionError("Source is outside QUEUE_INPUT_DIR") from exc
    stat = candidate.stat()
    if stat.st_size != source["file_size"] or stat.st_mtime_ns != source["mtime_ns"]:
        raise FileNotFoundError("Source VOD changed after discovery")
    return candidate
