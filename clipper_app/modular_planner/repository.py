from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence


SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class PlannerRepository:
    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

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
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS schema_meta(version INTEGER NOT NULL);
                INSERT INTO schema_meta(version)
                    SELECT 1 WHERE NOT EXISTS(SELECT 1 FROM schema_meta);

                CREATE TABLE IF NOT EXISTS modular_planner_runs(
                    planner_run_id TEXT PRIMARY KEY,
                    production_method TEXT NOT NULL CHECK(production_method='modular_video'),
                    product TEXT NOT NULL,
                    requested_template TEXT NOT NULL,
                    ingredient_shortage_policy TEXT NOT NULL,
                    cta_mode TEXT NOT NULL CHECK(cta_mode IN ('use_cta','no_cta')),
                    requested_count INTEGER NOT NULL CHECK(requested_count BETWEEN 1 AND 100),
                    target_min_duration REAL NOT NULL,
                    target_max_duration REAL NOT NULL,
                    seed TEXT NOT NULL,
                    planner_version TEXT NOT NULL,
                    inventory_snapshot_hash TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('draft','approved')),
                    revision INTEGER NOT NULL DEFAULT 1,
                    warnings_json TEXT NOT NULL DEFAULT '[]',
                    search_statistics_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    approved_at TEXT
                );

                CREATE TABLE IF NOT EXISTS modular_compositions(
                    composition_id TEXT PRIMARY KEY,
                    planner_run_id TEXT NOT NULL REFERENCES modular_planner_runs(planner_run_id),
                    ordinal INTEGER NOT NULL CHECK(ordinal >= 1),
                    requested_template TEXT NOT NULL,
                    actual_template TEXT NOT NULL,
                    fallback_reason TEXT,
                    cta_mode TEXT NOT NULL CHECK(cta_mode IN ('use_cta','no_cta')),
                    target_min_duration REAL NOT NULL,
                    target_max_duration REAL NOT NULL,
                    actual_duration REAL NOT NULL,
                    distinct_source_count INTEGER NOT NULL,
                    selection_score REAL NOT NULL,
                    selection_metadata_json TEXT NOT NULL DEFAULT '{}',
                    exact_signature TEXT NOT NULL,
                    near_signature TEXT NOT NULL,
                    signature_version TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('draft','approved','removed','superseded')),
                    supersedes_composition_id TEXT REFERENCES modular_compositions(composition_id),
                    created_at TEXT NOT NULL,
                    approved_at TEXT,
                    removed_at TEXT,
                    UNIQUE(planner_run_id, exact_signature)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_modular_approved_exact
                    ON modular_compositions(exact_signature) WHERE status='approved';
                CREATE UNIQUE INDEX IF NOT EXISTS idx_modular_active_ordinal
                    ON modular_compositions(planner_run_id, ordinal)
                    WHERE status IN ('draft','approved');
                CREATE INDEX IF NOT EXISTS idx_modular_compositions_run
                    ON modular_compositions(planner_run_id, status, ordinal);

                CREATE TABLE IF NOT EXISTS modular_composition_items(
                    composition_id TEXT NOT NULL REFERENCES modular_compositions(composition_id) ON DELETE CASCADE,
                    position INTEGER NOT NULL,
                    segment_id TEXT NOT NULL,
                    scan_id TEXT NOT NULL,
                    scanner_generation INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    source_filename TEXT NOT NULL,
                    canonical_path TEXT NOT NULL,
                    source_file_size INTEGER NOT NULL,
                    source_mtime_ns INTEGER NOT NULL,
                    source_content_fingerprint TEXT NOT NULL,
                    start_seconds REAL NOT NULL,
                    end_seconds REAL NOT NULL,
                    duration_seconds REAL NOT NULL,
                    confidence REAL NOT NULL,
                    transcript_text TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    approved_usage_at_selection INTEGER NOT NULL DEFAULT 0,
                    current_run_usage_at_selection INTEGER NOT NULL DEFAULT 0,
                    ranking_metadata_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY(composition_id, position)
                );
                CREATE INDEX IF NOT EXISTS idx_modular_items_segment
                    ON modular_composition_items(segment_id);
                CREATE INDEX IF NOT EXISTS idx_modular_items_source
                    ON modular_composition_items(source_id);

                CREATE TABLE IF NOT EXISTS modular_manifests(
                    manifest_id TEXT PRIMARY KEY,
                    planner_run_id TEXT NOT NULL UNIQUE REFERENCES modular_planner_runs(planner_run_id),
                    schema_version INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    checksum_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                COMMIT;
                """
            )
            row = db.execute("SELECT version FROM schema_meta LIMIT 1").fetchone()
            if row is None or int(row[0]) != SCHEMA_VERSION:
                raise RuntimeError(f"Unsupported modular planner schema version: {row[0] if row else 'missing'}")

    def create_run(self, values: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        with self.transaction() as db:
            db.execute(
                """INSERT INTO modular_planner_runs(
                    planner_run_id,production_method,product,requested_template,
                    ingredient_shortage_policy,cta_mode,requested_count,target_min_duration,
                    target_max_duration,seed,planner_version,inventory_snapshot_hash,status,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'draft', ?)""",
                (
                    values["planner_run_id"], "modular_video", values["product"], values["requested_template"],
                    values["ingredient_shortage_policy"], values["cta_mode"], values["requested_count"],
                    values["target_min_duration"], values["target_max_duration"], values["seed"],
                    values["planner_version"], values["inventory_snapshot_hash"], now,
                ),
            )
        return self.get_run(values["planner_run_id"])

    def finish_generation(self, run_id: str, warnings: Sequence[dict[str, Any]], statistics: dict[str, Any]) -> None:
        with self.transaction() as db:
            db.execute(
                """UPDATE modular_planner_runs SET warnings_json=?,search_statistics_json=?
                   WHERE planner_run_id=?""",
                (json.dumps(list(warnings), ensure_ascii=False), json.dumps(statistics), run_id),
            )

    def add_composition(
        self,
        run_id: str,
        composition: dict[str, Any],
        *,
        supersedes_id: str | None = None,
        expected_revision: int | None = None,
    ) -> str:
        composition_id = str(composition.get("composition_id") or uuid.uuid4().hex)
        now = utc_now()
        with self.transaction() as db:
            if expected_revision is not None:
                self._require_draft_revision(db, run_id, expected_revision)
            if supersedes_id is not None:
                old = db.execute(
                    "SELECT status FROM modular_compositions WHERE composition_id=? AND planner_run_id=?",
                    (supersedes_id, run_id),
                ).fetchone()
                if old is None or old["status"] != "draft":
                    raise ValueError("Only an active draft composition can be regenerated")
                db.execute(
                    "UPDATE modular_compositions SET status='superseded',removed_at=? WHERE composition_id=?",
                    (now, supersedes_id),
                )
            self._insert_composition(db, composition_id, run_id, composition, now, supersedes_id)
            db.execute("UPDATE modular_planner_runs SET revision=revision+1 WHERE planner_run_id=?", (run_id,))
        return composition_id

    @staticmethod
    def _insert_composition(
        db: sqlite3.Connection,
        composition_id: str,
        run_id: str,
        composition: dict[str, Any],
        now: str,
        supersedes_id: str | None,
    ) -> None:
        db.execute(
            """INSERT INTO modular_compositions(
                composition_id,planner_run_id,ordinal,requested_template,actual_template,
                fallback_reason,cta_mode,target_min_duration,target_max_duration,actual_duration,
                distinct_source_count,selection_score,selection_metadata_json,exact_signature,
                near_signature,signature_version,status,supersedes_composition_id,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'draft', ?,?)""",
            (
                composition_id, run_id, composition["ordinal"], composition["requested_template"],
                composition["actual_template"], composition.get("fallback_reason"), composition["cta_mode"],
                composition["target_min_duration"], composition["target_max_duration"],
                composition["actual_duration"], composition["distinct_source_count"],
                composition["selection_score"], json.dumps(composition.get("selection_metadata", {})),
                composition["exact_signature"], composition["near_signature"],
                composition["signature_version"], supersedes_id, now,
            ),
        )
        for position, item in enumerate(composition["items"]):
            db.execute(
                """INSERT INTO modular_composition_items(
                    composition_id,position,segment_id,scan_id,scanner_generation,role,source_id,
                    source_filename,canonical_path,source_file_size,source_mtime_ns,
                    source_content_fingerprint,start_seconds,end_seconds,duration_seconds,confidence,
                    transcript_text,reason,approved_usage_at_selection,current_run_usage_at_selection,
                    ranking_metadata_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    composition_id, position, item["segment_id"], item["scan_id"], item["scanner_generation"],
                    item["role"], item["source_id"], item["vod_filename"], item["canonical_path"],
                    item["file_size"], item["mtime_ns"], item["content_fingerprint"], item["start_seconds"],
                    item["end_seconds"], item["duration_seconds"], item["confidence"],
                    item["transcript_text"], item["reason"], item.get("approved_usage_at_selection", 0),
                    item.get("current_run_usage_at_selection", 0), json.dumps(item.get("ranking_metadata", {})),
                ),
            )

    def remove_composition(self, run_id: str, composition_id: str, expected_revision: int) -> None:
        now = utc_now()
        with self.transaction() as db:
            self._require_draft_revision(db, run_id, expected_revision)
            cursor = db.execute(
                """UPDATE modular_compositions SET status='removed',removed_at=?
                   WHERE composition_id=? AND planner_run_id=? AND status='draft'""",
                (now, composition_id, run_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Only an active draft composition can be removed")
            db.execute("UPDATE modular_planner_runs SET revision=revision+1 WHERE planner_run_id=?", (run_id,))

    def approved_usage(self, segment_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        if not segment_ids:
            return {}
        placeholders = ",".join("?" for _ in segment_ids)
        with closing(self.connect()) as db:
            rows = db.execute(
                f"""SELECT i.segment_id,COUNT(*) AS usage_count,MAX(c.approved_at) AS last_used_at
                    FROM modular_composition_items i JOIN modular_compositions c USING(composition_id)
                    WHERE c.status='approved' AND i.segment_id IN ({placeholders}) GROUP BY i.segment_id""",
                tuple(segment_ids),
            ).fetchall()
        return {row["segment_id"]: {"usage_count": row["usage_count"], "last_used_at": row["last_used_at"]} for row in rows}

    def comparison_compositions(self, run_id: str) -> list[dict[str, Any]]:
        with closing(self.connect()) as db:
            rows = db.execute(
                """SELECT * FROM modular_compositions
                   WHERE planner_run_id=? OR status='approved' ORDER BY created_at,composition_id""",
                (run_id,),
            ).fetchall()
            return [self._composition_with_items(db, row) for row in rows]

    def current_run_usage(self, run_id: str) -> dict[str, int]:
        with closing(self.connect()) as db:
            rows = db.execute(
                """SELECT i.segment_id,COUNT(*) AS usage_count
                   FROM modular_composition_items i JOIN modular_compositions c USING(composition_id)
                   WHERE c.planner_run_id=? GROUP BY i.segment_id""",
                (run_id,),
            ).fetchall()
        return {row["segment_id"]: int(row["usage_count"]) for row in rows}

    def get_run(self, run_id: str) -> dict[str, Any]:
        with closing(self.connect()) as db:
            row = db.execute("SELECT * FROM modular_planner_runs WHERE planner_run_id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError("Unknown planner run")
            result = dict(row)
            compositions = db.execute(
                "SELECT * FROM modular_compositions WHERE planner_run_id=? ORDER BY ordinal,created_at",
                (run_id,),
            ).fetchall()
            result["compositions"] = [self._composition_with_items(db, item) for item in compositions]
        result["warnings"] = json.loads(result.pop("warnings_json"))
        result["search_statistics"] = json.loads(result.pop("search_statistics_json"))
        active = [item for item in result["compositions"] if item["status"] in {"draft", "approved"}]
        result["generated_count"] = len(active)
        result["shortfall"] = max(0, int(result["requested_count"]) - len(active))
        return result

    def list_runs(self, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        clauses, params = [], []
        if status:
            clauses.append("status=?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with closing(self.connect()) as db:
            rows = db.execute(
                f"SELECT planner_run_id FROM modular_planner_runs {where} ORDER BY created_at DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        return [self.get_run(row["planner_run_id"]) for row in rows]

    @staticmethod
    def _composition_with_items(db: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["selection_metadata"] = json.loads(result.pop("selection_metadata_json"))
        items = db.execute(
            "SELECT * FROM modular_composition_items WHERE composition_id=? ORDER BY position",
            (result["composition_id"],),
        ).fetchall()
        result["items"] = []
        for item in items:
            value = dict(item)
            value["ranking_metadata"] = json.loads(value.pop("ranking_metadata_json"))
            result["items"].append(value)
        return result

    @staticmethod
    def _require_draft_revision(db: sqlite3.Connection, run_id: str, expected_revision: int) -> sqlite3.Row:
        row = db.execute("SELECT * FROM modular_planner_runs WHERE planner_run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError("Unknown planner run")
        if row["status"] != "draft":
            raise ValueError("Approved planner runs are immutable")
        if int(row["revision"]) != expected_revision:
            raise RuntimeError("Planner run revision conflict")
        return row

    def approve(self, run_id: str, expected_revision: int, manifest: dict[str, Any], checksum: str) -> str:
        now, manifest_id = utc_now(), uuid.uuid4().hex
        with self.transaction() as db:
            self._require_draft_revision(db, run_id, expected_revision)
            count = db.execute(
                "SELECT COUNT(*) FROM modular_compositions WHERE planner_run_id=? AND status='draft'",
                (run_id,),
            ).fetchone()[0]
            if not count:
                raise ValueError("At least one active draft composition is required")
            try:
                db.execute(
                    """UPDATE modular_compositions SET status='approved',approved_at=?
                       WHERE planner_run_id=? AND status='draft'""",
                    (now, run_id),
                )
            except sqlite3.IntegrityError as exc:
                raise RuntimeError("An exact composition has already been approved") from exc
            db.execute(
                """UPDATE modular_planner_runs SET status='approved',approved_at=?,revision=revision+1
                   WHERE planner_run_id=?""",
                (now, run_id),
            )
            db.execute(
                """INSERT INTO modular_manifests(
                    manifest_id,planner_run_id,schema_version,payload_json,checksum_sha256,created_at
                ) VALUES(?,?,1,?,?,?)""",
                (manifest_id, run_id, json.dumps(manifest, ensure_ascii=False, separators=(",", ":")), checksum, now),
            )
        return manifest_id

    def manifest(self, run_id: str) -> dict[str, Any]:
        with closing(self.connect()) as db:
            row = db.execute("SELECT * FROM modular_manifests WHERE planner_run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError("Approved manifest was not found")
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        return result
