from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from .models import LifecycleClass, PINNED_LIFECYCLES


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def stable_id(prefix: str, value: Any) -> str:
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest}"


class ArtifactRegistry:
    """Small authoritative registry for newly managed artifacts.

    Historical files need not be registered.  Callers must therefore treat a
    missing row as UNKNOWN/KEEP rather than as an orphan.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path).resolve(strict=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.initialize()

    @classmethod
    def from_working_dir(cls, working_dir: str | Path) -> "ArtifactRegistry":
        return cls(Path(working_dir) / "artifacts" / "artifact_registry.sqlite3")

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=15, check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=15000")
        return db

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock, closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise

    def initialize(self) -> None:
        with self._lock, closing(self.connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS artifacts(
                    artifact_id TEXT PRIMARY KEY,
                    artifact_type TEXT NOT NULL,
                    canonical_path TEXT NOT NULL UNIQUE,
                    size_bytes INTEGER NOT NULL DEFAULT 0,
                    content_identity TEXT,
                    fingerprint TEXT NOT NULL,
                    owner_identity TEXT,
                    lifecycle_class TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'AVAILABLE',
                    regenerable INTEGER,
                    regeneration_evidence_json TEXT NOT NULL DEFAULT '{}',
                    pinned INTEGER NOT NULL DEFAULT 0,
                    pin_reason TEXT,
                    created_at TEXT NOT NULL,
                    last_confirmed_use TEXT
                );
                CREATE TABLE IF NOT EXISTS artifact_references(
                    reference_id TEXT PRIMARY KEY,
                    artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
                    owner_type TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(artifact_id,owner_type,owner_id,role)
                );
                CREATE INDEX IF NOT EXISTS idx_artifact_refs_artifact_active
                    ON artifact_references(artifact_id,active);
                CREATE TABLE IF NOT EXISTS publish_operations(
                    operation_id TEXT PRIMARY KEY,
                    artifact_id TEXT,
                    source_path TEXT NOT NULL,
                    destination_path TEXT NOT NULL,
                    state TEXT NOT NULL,
                    size_bytes INTEGER,
                    content_identity TEXT,
                    lifecycle_class TEXT,
                    owner_identity TEXT,
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_publish_destination ON publish_operations(destination_path);
                CREATE TABLE IF NOT EXISTS raw_intermediates(
                    operation_id TEXT PRIMARY KEY,
                    path TEXT NOT NULL UNIQUE,
                    owner_job TEXT NOT NULL,
                    owner_stage TEXT NOT NULL,
                    successor_path TEXT,
                    status TEXT NOT NULL,
                    retry_required INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    cleanup_evidence_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS reconciliation_events(
                    event_id TEXT PRIMARY KEY,
                    domain TEXT NOT NULL,
                    record_id TEXT NOT NULL,
                    classification TEXT NOT NULL,
                    previous_path TEXT,
                    resolved_path TEXT,
                    reason TEXT NOT NULL,
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    applied INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    UNIQUE(domain,record_id,previous_path,resolved_path,classification)
                );
                CREATE TABLE IF NOT EXISTS inventory_cache(
                    path TEXT PRIMARY KEY,
                    root_name TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    category TEXT NOT NULL,
                    last_seen_scan TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_inventory_scan ON inventory_cache(last_seen_scan);
                """
            )
            db.commit()

    def register_artifact(
        self,
        *,
        artifact_id: str,
        artifact_type: str,
        canonical_path: str | Path,
        fingerprint: str,
        lifecycle_class: LifecycleClass | str,
        size_bytes: int | None = None,
        content_identity: str | None = None,
        owner_identity: str | None = None,
        regenerable: bool | None = None,
        regeneration_evidence: dict[str, Any] | None = None,
        pinned: bool | None = None,
        pin_reason: str | None = None,
        state: str = "AVAILABLE",
    ) -> None:
        path = Path(canonical_path).resolve(strict=False)
        lifecycle = LifecycleClass(str(lifecycle_class))
        if size_bytes is None:
            try:
                size_bytes = path.stat().st_size
            except OSError:
                size_bytes = 0
        should_pin = lifecycle in PINNED_LIFECYCLES if pinned is None else bool(pinned)
        now = utc_now()
        with self.transaction() as db:
            conflict = db.execute(
                "SELECT artifact_id FROM artifacts WHERE canonical_path=? AND artifact_id<>?",
                (str(path), artifact_id),
            ).fetchone()
            if conflict:
                db.execute(
                    "UPDATE artifacts SET canonical_path=canonical_path || '#superseded:' || artifact_id,state='SUPERSEDED' WHERE artifact_id=?",
                    (str(conflict[0]),),
                )
            db.execute(
                """INSERT INTO artifacts(
                       artifact_id,artifact_type,canonical_path,size_bytes,content_identity,fingerprint,
                       owner_identity,lifecycle_class,state,regenerable,regeneration_evidence_json,
                       pinned,pin_reason,created_at,last_confirmed_use
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(artifact_id) DO UPDATE SET
                       canonical_path=excluded.canonical_path,size_bytes=excluded.size_bytes,
                       content_identity=COALESCE(excluded.content_identity,artifacts.content_identity),
                       owner_identity=COALESCE(excluded.owner_identity,artifacts.owner_identity),
                       lifecycle_class=excluded.lifecycle_class,state=excluded.state,
                       regenerable=excluded.regenerable,
                       regeneration_evidence_json=excluded.regeneration_evidence_json,
                       pinned=excluded.pinned,pin_reason=excluded.pin_reason,
                       last_confirmed_use=excluded.last_confirmed_use""",
                (
                    artifact_id, artifact_type, str(path), int(size_bytes or 0), content_identity,
                    fingerprint, owner_identity, lifecycle.value, state,
                    None if regenerable is None else int(regenerable),
                    canonical_json(regeneration_evidence or {}), int(should_pin),
                    pin_reason or (lifecycle.value if should_pin else None), now, now,
                ),
            )

    def add_reference(
        self,
        artifact_id: str,
        *,
        owner_type: str,
        owner_id: str,
        role: str,
        active: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        reference_id = stable_id("ref", [artifact_id, owner_type, owner_id, role])
        now = utc_now()
        with self.transaction() as db:
            db.execute(
                """INSERT INTO artifact_references(
                       reference_id,artifact_id,owner_type,owner_id,role,active,metadata_json,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(artifact_id,owner_type,owner_id,role) DO UPDATE SET
                       active=excluded.active,metadata_json=excluded.metadata_json,updated_at=excluded.updated_at""",
                (reference_id, artifact_id, owner_type, owner_id, role, int(active),
                 canonical_json(metadata or {}), now, now),
            )
            db.execute("UPDATE artifacts SET last_confirmed_use=? WHERE artifact_id=?", (now, artifact_id))
        return reference_id

    def artifact_for_path(self, path: str | Path) -> dict[str, Any] | None:
        value = str(Path(path).resolve(strict=False))
        with closing(self.connect()) as db:
            row = db.execute("SELECT * FROM artifacts WHERE canonical_path=?", (value,)).fetchone()
        return dict(row) if row else None

    def artifact(self, artifact_id: str) -> dict[str, Any] | None:
        with closing(self.connect()) as db:
            row = db.execute("SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
        return dict(row) if row else None

    def active_references(self, artifact_id: str) -> list[dict[str, Any]]:
        with closing(self.connect()) as db:
            rows = db.execute(
                "SELECT * FROM artifact_references WHERE artifact_id=? AND active=1 ORDER BY owner_type,owner_id,role",
                (artifact_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def begin_publish(
        self, source: Path, destination: Path, *, artifact_id: str | None,
        lifecycle_class: LifecycleClass | str | None, owner_identity: str | None,
        size_bytes: int | None, content_identity: str | None, evidence: dict[str, Any] | None = None,
    ) -> str:
        operation_id = uuid4().hex
        with self.transaction() as db:
            db.execute(
                "INSERT INTO publish_operations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (operation_id, artifact_id, str(source), str(destination), "PREPARED", size_bytes,
                 content_identity, str(lifecycle_class) if lifecycle_class else None, owner_identity,
                 canonical_json(evidence or {}), utc_now(), None, None),
            )
        return operation_id

    def update_publish(self, operation_id: str, state: str, *, error: str | None = None) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE publish_operations SET state=?,completed_at=?,error=? WHERE operation_id=?",
                (state, utc_now() if state in {"COMMITTED", "FAILED", "ROLLED_BACK"} else None, error, operation_id),
            )

    def publish_history_for_path(self, path: str | Path) -> list[dict[str, Any]]:
        value = str(Path(path).resolve(strict=False))
        with closing(self.connect()) as db:
            rows = db.execute(
                "SELECT * FROM publish_operations WHERE source_path=? OR destination_path=? ORDER BY started_at DESC",
                (value, value),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_reconciliation(self, values: dict[str, Any]) -> None:
        payload = {
            "event_id": values.get("event_id") or uuid4().hex,
            "domain": values["domain"], "record_id": values["record_id"],
            "classification": values["classification"], "previous_path": values.get("previous_path"),
            "resolved_path": values.get("resolved_path"), "reason": values["reason"],
            "evidence_json": canonical_json(values.get("evidence") or {}),
            "applied": int(bool(values.get("applied"))), "created_at": values.get("created_at") or utc_now(),
        }
        with self.transaction() as db:
            db.execute(
                """INSERT INTO reconciliation_events(
                       event_id,domain,record_id,classification,previous_path,resolved_path,reason,
                       evidence_json,applied,created_at
                   ) VALUES(:event_id,:domain,:record_id,:classification,:previous_path,:resolved_path,:reason,
                            :evidence_json,:applied,:created_at)
                   ON CONFLICT(domain,record_id,previous_path,resolved_path,classification) DO UPDATE SET
                       reason=excluded.reason,evidence_json=excluded.evidence_json,
                       applied=MAX(reconciliation_events.applied,excluded.applied)""",
                payload,
            )

    def all_artifacts(self) -> list[dict[str, Any]]:
        with closing(self.connect()) as db:
            rows = db.execute("SELECT * FROM artifacts ORDER BY canonical_path").fetchall()
        return [dict(row) for row in rows]
