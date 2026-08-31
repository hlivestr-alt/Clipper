from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any


class ScannerLibraryReader:
    """Read-only adapter over the scanner-owned modular library."""

    def __init__(self, database_path: str | Path, vod_root: str | Path):
        self.database_path = Path(database_path).resolve()
        self.vod_root = Path(vod_root).resolve()

    def connect(self) -> sqlite3.Connection:
        if not self.database_path.is_file():
            raise FileNotFoundError(f"Modular scanner library was not found: {self.database_path}")
        connection = sqlite3.connect(
            f"file:{self.database_path.as_posix()}?mode=ro",
            uri=True,
            timeout=5.0,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def eligible_segments(self, product: str) -> list[dict[str, Any]]:
        with closing(self.connect()) as db:
            rows = db.execute(
                """SELECT seg.*, src.canonical_path, src.file_size, src.mtime_ns,
                          src.content_fingerprint, sc.generation AS scanner_generation,
                          sc.analyzer_version, sc.prompt_version
                   FROM segments seg
                   JOIN scans sc ON sc.scan_id=seg.scan_id
                   JOIN media_sources src ON src.source_id=seg.source_id
                   WHERE seg.product=? AND sc.status='completed'
                     AND sc.generation=(
                       SELECT MAX(sc2.generation) FROM scans sc2
                       WHERE sc2.source_id=sc.source_id AND sc2.status='completed'
                     )
                   ORDER BY seg.role, seg.confidence DESC, seg.segment_id""",
                (product,),
            ).fetchall()
        return [dict(row) for row in rows]

    def inventory(self, product: str) -> dict[str, Any]:
        rows = self.eligible_segments(product)
        roles: dict[str, dict[str, Any]] = {}
        for role in ("hook", "benefits", "ingredients", "cta"):
            selected = [row for row in rows if row["role"] == role]
            roles[role] = {
                "segments": len(selected),
                "distinct_sources": len({row["source_id"] for row in selected}),
                "minimum_duration": min((float(row["duration_seconds"]) for row in selected), default=None),
                "maximum_duration": max((float(row["duration_seconds"]) for row in selected), default=None),
            }
        digest_rows = [
            (row["segment_id"], row["scan_id"], row["source_id"], row["start_seconds"], row["end_seconds"])
            for row in rows
        ]
        snapshot_hash = hashlib.sha256(
            json.dumps(digest_rows, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return {"product": product, "roles": roles, "snapshot_hash": snapshot_hash, "segments": rows}

    def verify_source(self, snapshot: dict[str, Any]) -> str | None:
        path = Path(str(snapshot["canonical_path"])).resolve()
        try:
            path.relative_to(self.vod_root)
        except ValueError:
            return "source_outside_vod_root"
        if not path.is_file():
            return "source_missing"
        stat = path.stat()
        if int(stat.st_size) != int(snapshot["source_file_size"]):
            return "source_size_changed"
        if int(stat.st_mtime_ns) != int(snapshot["source_mtime_ns"]):
            return "source_mtime_changed"
        return None
