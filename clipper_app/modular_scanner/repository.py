from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 4
TRANSCRIPT_RECORD_SCHEMA_VERSION = 3


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ScannerRepository:
    """Small scanner-owned SQLite repository. It never opens the main catalog."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = self.connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def initialize(self) -> None:
        with self._lock, closing(self.connect()) as db:
            db.execute("PRAGMA journal_mode = WAL")
            try:
                db.executescript(
                    f"""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS schema_meta (
                    version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS media_sources (
                    source_id TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    canonical_path TEXT NOT NULL,
                    file_size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    content_fingerprint TEXT NOT NULL,
                    duration_seconds REAL,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    UNIQUE(canonical_path, file_size, mtime_ns, content_fingerprint)
                );
                CREATE TABLE IF NOT EXISTS transcripts (
                    transcript_id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL REFERENCES media_sources(source_id),
                    origin TEXT NOT NULL CHECK(origin IN ('scanner', 'production')),
                    cache_path TEXT NOT NULL,
                    transcript_fingerprint TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    UNIQUE(source_id, transcript_fingerprint, schema_version)
                );
                CREATE TABLE IF NOT EXISTS scans (
                    scan_id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL REFERENCES media_sources(source_id),
                    transcript_id TEXT REFERENCES transcripts(transcript_id),
                    generation INTEGER NOT NULL,
                    trigger TEXT NOT NULL CHECK(trigger IN ('scan', 'rescan')),
                    status TEXT NOT NULL,
                    analyzer_version TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    progress_current INTEGER NOT NULL DEFAULT 0,
                    progress_total INTEGER NOT NULL DEFAULT 0,
                    accepted_count INTEGER NOT NULL DEFAULT 0,
                    rejected_count INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    UNIQUE(source_id, generation)
                );
                CREATE TABLE IF NOT EXISTS scan_chunks (
                    scan_id TEXT NOT NULL REFERENCES scans(scan_id) ON DELETE CASCADE,
                    chunk_index INTEGER NOT NULL,
                    window_start REAL NOT NULL,
                    window_end REAL NOT NULL,
                    ownership_start REAL NOT NULL,
                    ownership_end REAL NOT NULL,
                    status TEXT NOT NULL,
                    response_json TEXT,
                    error TEXT,
                    completed_at TEXT,
                    PRIMARY KEY(scan_id, chunk_index)
                );
                CREATE TABLE IF NOT EXISTS segments (
                    segment_id TEXT PRIMARY KEY,
                    scan_id TEXT NOT NULL REFERENCES scans(scan_id) ON DELETE CASCADE,
                    source_id TEXT NOT NULL REFERENCES media_sources(source_id),
                    vod_filename TEXT NOT NULL,
                    product TEXT NOT NULL,
                    role TEXT NOT NULL,
                    start_seconds REAL NOT NULL,
                    end_seconds REAL NOT NULL,
                    duration_seconds REAL NOT NULL,
                    confidence REAL NOT NULL,
                    transcript_text TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    validation_diagnostics_json TEXT NOT NULL DEFAULT '{{}}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_segments_scan ON segments(scan_id, start_seconds);
                CREATE INDEX IF NOT EXISTS idx_segments_filter ON segments(product, role, confidence);
                CREATE TABLE IF NOT EXISTS scan_rejections (
                    rejection_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scan_id TEXT NOT NULL REFERENCES scans(scan_id) ON DELETE CASCADE,
                    chunk_index INTEGER,
                    reason_code TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    candidate_json TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS scan_batches (
                    batch_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    status TEXT NOT NULL DEFAULT 'preparing',
                    discovered_count INTEGER NOT NULL DEFAULT 0,
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS scan_batch_items (
                    batch_id TEXT NOT NULL REFERENCES scan_batches(batch_id) ON DELETE CASCADE,
                    source_id TEXT NOT NULL REFERENCES media_sources(source_id),
                    scan_id TEXT REFERENCES scans(scan_id),
                    disposition TEXT NOT NULL CHECK(disposition IN (
                        'queued', 'already_current', 'already_active', 'failed_to_queue'
                    )),
                    detail TEXT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(batch_id, source_id)
                );
                CREATE INDEX IF NOT EXISTS idx_scan_batch_items_scan ON scan_batch_items(scan_id);
                CREATE TABLE IF NOT EXISTS scan_batch_failures (
                    batch_id TEXT NOT NULL REFERENCES scan_batches(batch_id) ON DELETE CASCADE,
                    item_key TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(batch_id, item_key)
                );
                CREATE INDEX IF NOT EXISTS idx_scans_source ON scans(source_id, generation DESC);
                INSERT INTO schema_meta(version)
                SELECT {SCHEMA_VERSION} WHERE NOT EXISTS (SELECT 1 FROM schema_meta);
                COMMIT;
                """
                )
            except Exception:
                db.rollback()
                raise
            row = db.execute("SELECT version FROM schema_meta LIMIT 1").fetchone()
            if row is not None and int(row[0]) in {1, 2, 3}:
                if int(row[0]) == 1:
                    columns = {item[1] for item in db.execute("PRAGMA table_info(segments)").fetchall()}
                    if "validation_diagnostics_json" not in columns:
                        db.execute("ALTER TABLE segments ADD COLUMN validation_diagnostics_json TEXT NOT NULL DEFAULT '{}'")
                if int(row[0]) <= 3:
                    batch_columns = {item[1] for item in db.execute("PRAGMA table_info(scan_batches)").fetchall()}
                    if "status" not in batch_columns:
                        db.execute("ALTER TABLE scan_batches ADD COLUMN status TEXT NOT NULL DEFAULT 'preparing'")
                    if "discovered_count" not in batch_columns:
                        db.execute("ALTER TABLE scan_batches ADD COLUMN discovered_count INTEGER NOT NULL DEFAULT 0")
                    if "error" not in batch_columns:
                        db.execute("ALTER TABLE scan_batches ADD COLUMN error TEXT")
                    db.execute(
                        """UPDATE scan_batches SET
                        status=CASE WHEN completed_at IS NULL THEN 'running' ELSE 'completed' END,
                        discovered_count=(SELECT COUNT(*) FROM scan_batch_items i WHERE i.batch_id=scan_batches.batch_id)
                        """
                    )
                db.execute("UPDATE schema_meta SET version=?", (SCHEMA_VERSION,))
                db.commit()
                row = db.execute("SELECT version FROM schema_meta LIMIT 1").fetchone()
            if row is None or int(row[0]) != SCHEMA_VERSION:
                found = row[0] if row is not None else "missing"
                raise RuntimeError(f"Unsupported modular scanner schema version: {found}")

    @staticmethod
    def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def upsert_source(self, source: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        with self.transaction() as db:
            db.execute(
                """INSERT INTO media_sources(
                    source_id, filename, canonical_path, file_size, mtime_ns,
                    content_fingerprint, duration_seconds, created_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    duration_seconds=COALESCE(excluded.duration_seconds, media_sources.duration_seconds),
                    last_seen_at=excluded.last_seen_at""",
                (
                    source["source_id"], source["filename"], source["canonical_path"],
                    source["file_size"], source["mtime_ns"], source["content_fingerprint"],
                    source.get("duration_seconds"), now, now,
                ),
            )
            row = db.execute("SELECT * FROM media_sources WHERE source_id=?", (source["source_id"],)).fetchone()
        return dict(row)

    def get_source(self, source_id: str) -> dict[str, Any] | None:
        with closing(self.connect()) as db:
            return self._dict(db.execute("SELECT * FROM media_sources WHERE source_id=?", (source_id,)).fetchone())

    def source_by_metadata(self, canonical_path: str, file_size: int, mtime_ns: int) -> dict[str, Any] | None:
        with closing(self.connect()) as db:
            row = db.execute(
                """SELECT * FROM media_sources
                WHERE canonical_path=? AND file_size=? AND mtime_ns=?
                ORDER BY last_seen_at DESC LIMIT 1""",
                (canonical_path, file_size, mtime_ns),
            ).fetchone()
        return self._dict(row)

    def add_transcript(self, source_id: str, origin: str, cache_path: str, fingerprint: str) -> dict[str, Any]:
        transcript_id = uuid.uuid4().hex
        now = utc_now()
        with self.transaction() as db:
            db.execute(
                """INSERT OR IGNORE INTO transcripts(
                    transcript_id, source_id, origin, cache_path, transcript_fingerprint,
                    schema_version, status, created_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'completed', ?, ?)""",
                (
                    transcript_id, source_id, origin, cache_path, fingerprint,
                    TRANSCRIPT_RECORD_SCHEMA_VERSION, now, now,
                ),
            )
            row = db.execute(
                "SELECT * FROM transcripts WHERE source_id=? AND transcript_fingerprint=? AND schema_version=?",
                (source_id, fingerprint, TRANSCRIPT_RECORD_SCHEMA_VERSION),
            ).fetchone()
        return dict(row)

    def compatible_transcript(self, source_id: str) -> dict[str, Any] | None:
        with closing(self.connect()) as db:
            row = db.execute(
                """SELECT * FROM transcripts WHERE source_id=? AND status='completed'
                ORDER BY completed_at DESC LIMIT 1""",
                (source_id,),
            ).fetchone()
        return self._dict(row)

    def get_transcript(self, transcript_id: str) -> dict[str, Any] | None:
        with closing(self.connect()) as db:
            row = db.execute("SELECT * FROM transcripts WHERE transcript_id=?", (transcript_id,)).fetchone()
        return self._dict(row)

    def create_scan(self, source_id: str, trigger: str, analyzer_version: str, prompt_version: str, model_id: str) -> dict[str, Any]:
        scan_id = uuid.uuid4().hex
        now = utc_now()
        with self.transaction() as db:
            generation = int(db.execute("SELECT COALESCE(MAX(generation), 0) + 1 FROM scans WHERE source_id=?", (source_id,)).fetchone()[0])
            db.execute(
                """INSERT INTO scans(
                    scan_id, source_id, generation, trigger, status, analyzer_version,
                    prompt_version, model_id, created_at
                ) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?)""",
                (scan_id, source_id, generation, trigger, analyzer_version, prompt_version, model_id, now),
            )
            row = db.execute("SELECT * FROM scans WHERE scan_id=?", (scan_id,)).fetchone()
        return dict(row)

    def get_scan(self, scan_id: str) -> dict[str, Any] | None:
        with closing(self.connect()) as db:
            return self._dict(db.execute("SELECT * FROM scans WHERE scan_id=?", (scan_id,)).fetchone())

    def list_scans(self, source_id: str) -> list[dict[str, Any]]:
        with closing(self.connect()) as db:
            rows = db.execute("SELECT * FROM scans WHERE source_id=? ORDER BY generation DESC", (source_id,)).fetchall()
        return [dict(row) for row in rows]

    def current_scan(self, source_id: str) -> dict[str, Any] | None:
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT * FROM scans WHERE source_id=? AND status='completed' ORDER BY generation DESC LIMIT 1",
                (source_id,),
            ).fetchone()
        return self._dict(row)

    def active_scan(self, source_id: str) -> dict[str, Any] | None:
        with closing(self.connect()) as db:
            row = db.execute(
                """SELECT * FROM scans WHERE source_id=? AND status IN (
                    'queued', 'waiting_for_production', 'transcribing', 'analyzing', 'validating'
                ) ORDER BY generation DESC LIMIT 1""",
                (source_id,),
            ).fetchone()
        return self._dict(row)

    def pending_scans(self) -> list[dict[str, Any]]:
        with closing(self.connect()) as db:
            rows = db.execute("SELECT * FROM scans WHERE status='queued' ORDER BY created_at").fetchall()
        return [dict(row) for row in rows]

    def compatible_scan(self, source_id: str, transcript_fingerprint: str, analyzer_version: str, prompt_version: str, model_id: str) -> dict[str, Any] | None:
        with closing(self.connect()) as db:
            row = db.execute(
                """SELECT s.* FROM scans s JOIN transcripts t ON t.transcript_id=s.transcript_id
                WHERE s.source_id=? AND s.status='completed' AND t.transcript_fingerprint=?
                AND s.analyzer_version=? AND s.prompt_version=? AND s.model_id=?
                ORDER BY s.generation DESC LIMIT 1""",
                (source_id, transcript_fingerprint, analyzer_version, prompt_version, model_id),
            ).fetchone()
        return self._dict(row)

    def update_scan(self, scan_id: str, status: str, **fields: Any) -> None:
        allowed = {
            "transcript_id", "progress_current", "progress_total", "accepted_count",
            "rejected_count", "error", "started_at", "completed_at",
        }
        values = {key: value for key, value in fields.items() if key in allowed}
        assignments = ["status=?", *[f"{key}=?" for key in values]]
        params = [status, *values.values(), scan_id]
        with self.transaction() as db:
            db.execute(f"UPDATE scans SET {', '.join(assignments)} WHERE scan_id=?", params)

    def upsert_chunks(self, scan_id: str, windows: list[dict[str, Any]]) -> None:
        with self.transaction() as db:
            for window in windows:
                db.execute(
                    """INSERT OR IGNORE INTO scan_chunks(
                        scan_id, chunk_index, window_start, window_end, ownership_start,
                        ownership_end, status
                    ) VALUES (?, ?, ?, ?, ?, ?, 'queued')""",
                    (scan_id, window["index"], window["start"], window["end"], window["ownership_start"], window["ownership_end"]),
                )

    def chunks(self, scan_id: str) -> list[dict[str, Any]]:
        with closing(self.connect()) as db:
            rows = db.execute("SELECT * FROM scan_chunks WHERE scan_id=? ORDER BY chunk_index", (scan_id,)).fetchall()
        return [dict(row) for row in rows]

    def complete_chunk(self, scan_id: str, chunk_index: int, candidates: list[dict[str, Any]]) -> None:
        with self.transaction() as db:
            db.execute(
                """UPDATE scan_chunks SET status='completed', response_json=?, error=NULL, completed_at=?
                WHERE scan_id=? AND chunk_index=?""",
                (json.dumps(candidates, ensure_ascii=False), utc_now(), scan_id, chunk_index),
            )

    def fail_chunk(self, scan_id: str, chunk_index: int, error: str) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE scan_chunks SET status='failed', error=? WHERE scan_id=? AND chunk_index=?",
                (error[:1000], scan_id, chunk_index),
            )

    def replace_segments(self, scan_id: str, source: dict[str, Any], segments: list[dict[str, Any]]) -> None:
        with self.transaction() as db:
            db.execute("DELETE FROM segments WHERE scan_id=?", (scan_id,))
            for segment in segments:
                db.execute(
                    """INSERT INTO segments(
                        segment_id, scan_id, source_id, vod_filename, product, role,
                        start_seconds, end_seconds, duration_seconds, confidence,
                        transcript_text, reason, validation_diagnostics_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        uuid.uuid4().hex, scan_id, source["source_id"], source["filename"],
                        segment["product"], segment["role"], segment["start_seconds"],
                        segment["end_seconds"], segment["duration_seconds"], segment["confidence"],
                        segment["transcript_text"], segment["reason"],
                        json.dumps(segment.get("validation_diagnostics") or {}, ensure_ascii=False), utc_now(),
                    ),
                )

    def add_rejection(self, scan_id: str, chunk_index: int | None, reason_code: str, detail: str, candidate: Any) -> None:
        with self.transaction() as db:
            count = int(db.execute("SELECT COUNT(*) FROM scan_rejections WHERE scan_id=?", (scan_id,)).fetchone()[0])
            if count >= 500:
                return
            db.execute(
                """INSERT INTO scan_rejections(scan_id, chunk_index, reason_code, detail, candidate_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (scan_id, chunk_index, reason_code, detail[:500], json.dumps(candidate, ensure_ascii=False)[:4000], utc_now()),
            )

    def create_batch(self, items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        batch_id = uuid.uuid4().hex
        now = utc_now()
        with self.transaction() as db:
            db.execute(
                "INSERT INTO scan_batches(batch_id, created_at, status) VALUES (?, ?, 'preparing')",
                (batch_id, now),
            )
            for item in items or []:
                db.execute(
                    """INSERT INTO scan_batch_items(
                        batch_id, source_id, scan_id, disposition, detail, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        batch_id, item["source_id"], item.get("scan_id"), item["disposition"],
                        str(item.get("detail") or "")[:500] or None, now,
                    ),
                )
        return self.get_batch(batch_id) or {"batch_id": batch_id, "created_at": now, "completed_at": None}

    def active_batch(self) -> dict[str, Any] | None:
        with closing(self.connect()) as db:
            row = db.execute(
                """SELECT * FROM scan_batches
                WHERE status IN ('preparing', 'running')
                ORDER BY created_at DESC LIMIT 1"""
            ).fetchone()
        return self._dict(row)

    def pending_batches(self) -> list[dict[str, Any]]:
        with closing(self.connect()) as db:
            rows = db.execute(
                "SELECT * FROM scan_batches WHERE status='preparing' ORDER BY created_at"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        with closing(self.connect()) as db:
            row = db.execute("SELECT * FROM scan_batches WHERE batch_id=?", (batch_id,)).fetchone()
        return self._dict(row)

    def batch_items(self, batch_id: str) -> list[dict[str, Any]]:
        with closing(self.connect()) as db:
            rows = db.execute(
                """SELECT i.*, m.filename, s.status AS scan_status, s.error AS scan_error
                FROM scan_batch_items i
                JOIN media_sources m ON m.source_id=i.source_id
                LEFT JOIN scans s ON s.scan_id=i.scan_id
                WHERE i.batch_id=? ORDER BY m.filename COLLATE NOCASE, i.source_id""",
                (batch_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def add_batch_item(self, batch_id: str, item: dict[str, Any]) -> None:
        with self.transaction() as db:
            db.execute(
                """INSERT INTO scan_batch_items(
                    batch_id, source_id, scan_id, disposition, detail, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(batch_id, source_id) DO UPDATE SET
                    scan_id=excluded.scan_id,
                    disposition=excluded.disposition,
                    detail=excluded.detail""",
                (
                    batch_id, item["source_id"], item.get("scan_id"), item["disposition"],
                    str(item.get("detail") or "")[:500] or None, utc_now(),
                ),
            )

    def add_batch_failure(self, batch_id: str, item_key: str, filename: str, detail: str) -> None:
        with self.transaction() as db:
            db.execute(
                """INSERT INTO scan_batch_failures(batch_id, item_key, filename, detail, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(batch_id, item_key) DO UPDATE SET detail=excluded.detail""",
                (batch_id, item_key, filename, detail[:500], utc_now()),
            )

    def batch_failures(self, batch_id: str) -> list[dict[str, Any]]:
        with closing(self.connect()) as db:
            rows = db.execute(
                "SELECT * FROM scan_batch_failures WHERE batch_id=? ORDER BY filename COLLATE NOCASE, item_key",
                (batch_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def batch_prepared_paths(self, batch_id: str) -> set[str]:
        with closing(self.connect()) as db:
            rows = db.execute(
                """SELECT m.canonical_path AS item_key
                FROM scan_batch_items i JOIN media_sources m ON m.source_id=i.source_id
                WHERE i.batch_id=?
                UNION SELECT item_key FROM scan_batch_failures WHERE batch_id=?""",
                (batch_id, batch_id),
            ).fetchall()
        return {str(row["item_key"]).casefold() for row in rows}

    def update_batch(self, batch_id: str, status: str, **fields: Any) -> None:
        allowed = {"discovered_count", "error", "completed_at"}
        values = {key: value for key, value in fields.items() if key in allowed}
        assignments = ["status=?", *[f"{key}=?" for key in values]]
        with self.transaction() as db:
            db.execute(
                f"UPDATE scan_batches SET {', '.join(assignments)} WHERE batch_id=?",
                (status, *values.values(), batch_id),
            )

    def complete_batch(self, batch_id: str, *, with_failures: bool = False) -> None:
        with self.transaction() as db:
            db.execute(
                """UPDATE scan_batches SET status=?, completed_at=COALESCE(completed_at, ?)
                WHERE batch_id=?""",
                ("completed_with_failures" if with_failures else "completed", utc_now(), batch_id),
            )

    def list_segments(self, scan_id: str, product: str | None = None, role: str | None = None, minimum_confidence: float = 0.0, search: str = "", sort: str = "timestamp") -> list[dict[str, Any]]:
        clauses = ["scan_id=?", "confidence>=?"]
        params: list[Any] = [scan_id, minimum_confidence]
        if product:
            clauses.append("product=?")
            params.append(product)
        if role:
            clauses.append("role=?")
            params.append(role)
        if search:
            clauses.append("(transcript_text LIKE ? OR reason LIKE ?)")
            params.extend([f"%{search}%", f"%{search}%"])
        order = {
            "timestamp": "start_seconds ASC",
            "duration": "duration_seconds DESC, start_seconds ASC",
            "confidence": "confidence DESC, start_seconds ASC",
        }.get(sort, "start_seconds ASC")
        with closing(self.connect()) as db:
            rows = db.execute(f"SELECT * FROM segments WHERE {' AND '.join(clauses)} ORDER BY {order}", params).fetchall()
        return [
            {key: value for key, value in dict(row).items() if key != "validation_diagnostics_json"}
            for row in rows
        ]

    def recover_incomplete(self) -> int:
        with self.transaction() as db:
            cursor = db.execute(
                """UPDATE scans SET status='queued', error=NULL
                WHERE status IN ('transcribing', 'analyzing', 'validating', 'waiting_for_production')"""
            )
            db.execute("UPDATE scan_chunks SET status='queued', error=NULL WHERE status='failed'")
            return cursor.rowcount
