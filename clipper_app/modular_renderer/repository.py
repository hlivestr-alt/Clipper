from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence


SCHEMA_VERSION = 2
ACTIVE_RUN_STATUSES = ("queued", "waiting_for_production", "rendering")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class RendererRepository:
    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5.0, check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=5000")
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
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS schema_meta(version INTEGER NOT NULL);
                INSERT INTO schema_meta(version)
                    SELECT 1 WHERE NOT EXISTS(SELECT 1 FROM schema_meta);

                CREATE TABLE IF NOT EXISTS modular_render_runs(
                    render_run_id TEXT PRIMARY KEY,
                    planner_run_id TEXT NOT NULL,
                    planner_manifest_id TEXT NOT NULL,
                    planner_manifest_checksum TEXT NOT NULL,
                    renderer_version TEXT NOT NULL,
                    request_key TEXT NOT NULL,
                    selected_composition_ids_json TEXT NOT NULL,
                    output_directory TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    status TEXT NOT NULL CHECK(status IN (
                        'queued','waiting_for_production','rendering','completed','partial_failure','failed'
                    )),
                    requested_count INTEGER NOT NULL,
                    succeeded_count INTEGER NOT NULL DEFAULT 0,
                    failed_count INTEGER NOT NULL DEFAULT 0,
                    current_composition_id TEXT,
                    rerender_of_run_id TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_modular_render_runs_planner
                    ON modular_render_runs(planner_run_id,created_at DESC);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_modular_render_active_request
                    ON modular_render_runs(request_key)
                    WHERE status IN ('queued','waiting_for_production','rendering');

                CREATE TABLE IF NOT EXISTS modular_render_items(
                    render_run_id TEXT NOT NULL REFERENCES modular_render_runs(render_run_id) ON DELETE CASCADE,
                    composition_id TEXT NOT NULL,
                    product TEXT NOT NULL,
                    template TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    renderer_version TEXT NOT NULL,
                    output_path TEXT NOT NULL,
                    expected_duration REAL NOT NULL,
                    rendered_duration REAL,
                    duration_delta REAL,
                    status TEXT NOT NULL CHECK(status IN (
                        'queued','waiting_for_production','rendering','completed','failed'
                    )),
                    error_code TEXT,
                    error_message TEXT,
                    source_verification_seconds REAL,
                    extraction_seconds REAL,
                    concat_encode_seconds REAL,
                    total_seconds REAL,
                    normalization_json TEXT NOT NULL DEFAULT '{}',
                    diagnostics_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    PRIMARY KEY(render_run_id,composition_id)
                );
                CREATE INDEX IF NOT EXISTS idx_modular_render_items_composition
                    ON modular_render_items(composition_id,status,created_at DESC);
                COMMIT;
                """
            )
            row = db.execute("SELECT version FROM schema_meta LIMIT 1").fetchone()
            version = int(row[0]) if row is not None else 0
            if version == 1:
                columns = {str(column[1]) for column in db.execute("PRAGMA table_info(modular_render_items)")}
                if "diagnostics_json" not in columns:
                    db.execute(
                        "ALTER TABLE modular_render_items ADD COLUMN diagnostics_json TEXT NOT NULL DEFAULT '{}'"
                    )
                db.execute("UPDATE schema_meta SET version=2")
                db.commit()
                version = 2
            if version != SCHEMA_VERSION:
                raise RuntimeError("Unsupported modular renderer schema version")

    def recover_incomplete(self) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE modular_render_items SET status='queued',error_code=NULL,error_message=NULL "
                "WHERE status IN ('rendering','waiting_for_production')"
            )
            db.execute(
                "UPDATE modular_render_runs SET status='queued',current_composition_id=NULL "
                "WHERE status IN ('rendering','waiting_for_production')"
            )

    def pending_run_ids(self) -> list[str]:
        with closing(self.connect()) as db:
            rows = db.execute(
                "SELECT render_run_id FROM modular_render_runs WHERE status='queued' ORDER BY created_at"
            ).fetchall()
        return [str(row[0]) for row in rows]

    def find_reusable(self, request_key: str) -> dict[str, Any] | None:
        with closing(self.connect()) as db:
            row = db.execute(
                """SELECT render_run_id FROM modular_render_runs
                   WHERE request_key=? AND status IN ('queued','waiting_for_production','rendering','completed')
                   ORDER BY CASE status WHEN 'completed' THEN 1 ELSE 0 END,created_at DESC LIMIT 1""",
                (request_key,),
            ).fetchone()
        return self.get_run(str(row[0])) if row else None

    def find_composition_overlap(
        self,
        manifest_id: str,
        renderer_version: str,
        composition_ids: Sequence[str],
        statuses: Sequence[str],
        *,
        item_status: str | None = None,
    ) -> dict[str, Any] | None:
        if not composition_ids or not statuses:
            return None
        composition_marks = ",".join("?" for _ in composition_ids)
        status_marks = ",".join("?" for _ in statuses)
        with closing(self.connect()) as db:
            item_clause = " AND i.status=?" if item_status else ""
            params: tuple[Any, ...] = (
                manifest_id, renderer_version, *composition_ids, *statuses,
                *((item_status,) if item_status else ()),
            )
            row = db.execute(
                f"""SELECT r.render_run_id FROM modular_render_runs r
                    JOIN modular_render_items i USING(render_run_id)
                    WHERE r.planner_manifest_id=? AND r.renderer_version=?
                      AND i.composition_id IN ({composition_marks}) AND r.status IN ({status_marks})
                      {item_clause}
                    ORDER BY r.created_at DESC LIMIT 1""",
                params,
            ).fetchone()
        return self.get_run(str(row[0])) if row else None

    def create_run(self, run: dict[str, Any], items: Sequence[dict[str, Any]]) -> dict[str, Any]:
        now = utc_now()
        with self.transaction() as db:
            db.execute(
                """INSERT INTO modular_render_runs(
                    render_run_id,planner_run_id,planner_manifest_id,planner_manifest_checksum,
                    renderer_version,request_key,selected_composition_ids_json,output_directory,
                    created_at,status,requested_count,rerender_of_run_id
                ) VALUES(?,?,?,?,?,?,?,?,?,'queued',?,?)""",
                (
                    run["render_run_id"], run["planner_run_id"], run["planner_manifest_id"],
                    run["planner_manifest_checksum"], run["renderer_version"], run["request_key"],
                    json.dumps(run["selected_composition_ids"]), run["output_directory"], now,
                    len(items), run.get("rerender_of_run_id"),
                ),
            )
            for item in items:
                db.execute(
                    """INSERT INTO modular_render_items(
                        render_run_id,composition_id,product,template,ordinal,renderer_version,
                        output_path,expected_duration,status,created_at
                    ) VALUES(?,?,?,?,?,?,?,?, 'queued',?)""",
                    (
                        run["render_run_id"], item["composition_id"], item["product"], item["template"],
                        item["ordinal"], run["renderer_version"], item["output_path"],
                        item["expected_duration"], now,
                    ),
                )
        return self.get_run(run["render_run_id"])

    def set_run_state(self, run_id: str, status: str, composition_id: str | None = None) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE modular_render_runs SET status=?,current_composition_id=? WHERE render_run_id=?",
                (status, composition_id, run_id),
            )

    def set_item_state(self, run_id: str, composition_id: str, status: str) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE modular_render_items SET status=? WHERE render_run_id=? AND composition_id=?",
                (status, run_id, composition_id),
            )

    def complete_item(self, run_id: str, composition_id: str, result: dict[str, Any]) -> None:
        with self.transaction() as db:
            db.execute(
                """UPDATE modular_render_items SET status='completed',rendered_duration=?,duration_delta=?,
                   error_code=NULL,error_message=NULL,source_verification_seconds=?,extraction_seconds=?,
                   concat_encode_seconds=?,total_seconds=?,normalization_json=?,diagnostics_json=?,completed_at=?
                   WHERE render_run_id=? AND composition_id=?""",
                (
                    result["rendered_duration"], result["duration_delta"],
                    result.get("source_verification_seconds"), result.get("extraction_seconds"),
                    result.get("concat_encode_seconds"), result.get("total_seconds"),
                    json.dumps(result.get("normalization", {}), ensure_ascii=False),
                    json.dumps(result.get("diagnostics", {}), ensure_ascii=False),
                    utc_now(), run_id, composition_id,
                ),
            )

    def fail_item(self, run_id: str, composition_id: str, code: str, message: str, total: float) -> None:
        with self.transaction() as db:
            db.execute(
                """UPDATE modular_render_items SET status='failed',error_code=?,error_message=?,
                   total_seconds=?,completed_at=? WHERE render_run_id=? AND composition_id=?""",
                (code, message[:2000], total, utc_now(), run_id, composition_id),
            )

    def finish_run(self, run_id: str) -> None:
        with self.transaction() as db:
            counts = db.execute(
                """SELECT COUNT(*) total,
                   SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) succeeded,
                   SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) failed
                   FROM modular_render_items WHERE render_run_id=?""",
                (run_id,),
            ).fetchone()
            succeeded, failed = int(counts["succeeded"] or 0), int(counts["failed"] or 0)
            status = "completed" if failed == 0 else ("partial_failure" if succeeded else "failed")
            db.execute(
                """UPDATE modular_render_runs SET status=?,succeeded_count=?,failed_count=?,
                   current_composition_id=NULL,completed_at=? WHERE render_run_id=?""",
                (status, succeeded, failed, utc_now(), run_id),
            )

    def get_run(self, run_id: str) -> dict[str, Any]:
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT * FROM modular_render_runs WHERE render_run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError("Unknown modular render run")
            result = dict(row)
            items = db.execute(
                "SELECT * FROM modular_render_items WHERE render_run_id=? ORDER BY ordinal",
                (run_id,),
            ).fetchall()
        result["selected_composition_ids"] = json.loads(result.pop("selected_composition_ids_json"))
        result["items"] = [self._public_item(dict(item)) for item in items]
        return result

    def list_runs(self, planner_run_id: str, limit: int = 20) -> list[dict[str, Any]]:
        with closing(self.connect()) as db:
            rows = db.execute(
                """SELECT render_run_id FROM modular_render_runs WHERE planner_run_id=?
                   ORDER BY created_at DESC LIMIT ?""",
                (planner_run_id, limit),
            ).fetchall()
        return [self.get_run(str(row[0])) for row in rows]

    def item(self, run_id: str, composition_id: str) -> dict[str, Any]:
        with closing(self.connect()) as db:
            row = db.execute(
                """SELECT i.*,r.output_directory FROM modular_render_items i
                   JOIN modular_render_runs r USING(render_run_id)
                   WHERE i.render_run_id=? AND i.composition_id=?""",
                (run_id, composition_id),
            ).fetchone()
        if row is None:
            raise KeyError("Unknown modular render item")
        return dict(row)

    @staticmethod
    def _public_item(item: dict[str, Any]) -> dict[str, Any]:
        item["normalization"] = json.loads(item.pop("normalization_json"))
        item["diagnostics"] = json.loads(item.pop("diagnostics_json", "{}"))
        item.pop("output_path", None)
        return item
