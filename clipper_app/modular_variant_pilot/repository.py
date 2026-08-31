from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence


ACTIVE = ("queued", "waiting_for_production", "generating")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class ModularVariantPilotRepository:
    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
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
            db.executescript("""
                CREATE TABLE IF NOT EXISTS modular_variant_runs(
                    run_id TEXT PRIMARY KEY, request_key TEXT NOT NULL, profile_id TEXT NOT NULL,
                    profile_revision TEXT NOT NULL, profile_json TEXT NOT NULL,
                    output_directory TEXT NOT NULL, created_at TEXT NOT NULL, completed_at TEXT,
                    status TEXT NOT NULL CHECK(status IN ('queued','waiting_for_production','generating','completed','partial_failure','failed')),
                    requested_base_count INTEGER NOT NULL, requested_variant_count INTEGER NOT NULL,
                    succeeded_base_count INTEGER NOT NULL DEFAULT 0, failed_base_count INTEGER NOT NULL DEFAULT 0,
                    total_expected_outputs INTEGER NOT NULL, total_completed_outputs INTEGER NOT NULL DEFAULT 0,
                    current_render_item_id TEXT, rerun_of_run_id TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS modular_variant_active_request
                    ON modular_variant_runs(request_key) WHERE status IN ('queued','waiting_for_production','generating');
                CREATE TABLE IF NOT EXISTS modular_variant_items(
                    run_id TEXT NOT NULL REFERENCES modular_variant_runs(run_id) ON DELETE CASCADE,
                    render_run_id TEXT NOT NULL, modular_render_item_id TEXT NOT NULL,
                    planner_run_id TEXT NOT NULL, composition_id TEXT NOT NULL, product TEXT NOT NULL,
                    renderer_version TEXT NOT NULL, base_path TEXT NOT NULL, base_identity TEXT NOT NULL,
                    ordinal INTEGER NOT NULL, variant_profile TEXT NOT NULL,
                    transcript_words_json TEXT NOT NULL DEFAULT '[]', hook_text TEXT NOT NULL DEFAULT '',
                    output_directory TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('queued','waiting_for_production','generating','completed','failed')),
                    expected_variant_count INTEGER NOT NULL, produced_variant_count INTEGER NOT NULL DEFAULT 0,
                    error TEXT, started_at TEXT, completed_at TEXT, generation_seconds REAL,
                    PRIMARY KEY(run_id,modular_render_item_id)
                );
                CREATE TABLE IF NOT EXISTS modular_variant_outputs(
                    media_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, modular_render_item_id TEXT NOT NULL,
                    variant_index INTEGER NOT NULL, variant_id TEXT NOT NULL, variant_name TEXT NOT NULL,
                    output_path TEXT NOT NULL, duration REAL NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL,
                    has_video INTEGER NOT NULL, has_audio INTEGER NOT NULL, file_size INTEGER NOT NULL,
                    generation_seconds REAL NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE(run_id,modular_render_item_id,variant_index)
                );
            """)
            run_columns = {row[1] for row in db.execute("PRAGMA table_info(modular_variant_runs)")}
            if "transcript_bridge_version" not in run_columns:
                db.execute("ALTER TABLE modular_variant_runs ADD COLUMN transcript_bridge_version TEXT NOT NULL DEFAULT 'legacy-manifest-synthetic'")
            item_columns = {row[1] for row in db.execute("PRAGMA table_info(modular_variant_items)")}
            if "transcript_diagnostics_json" not in item_columns:
                db.execute("ALTER TABLE modular_variant_items ADD COLUMN transcript_diagnostics_json TEXT NOT NULL DEFAULT '{}'")

    def recover_incomplete(self) -> None:
        with self.transaction() as db:
            db.execute("UPDATE modular_variant_items SET status='queued',error=NULL WHERE status IN ('generating','waiting_for_production')")
            db.execute("UPDATE modular_variant_runs SET status='queued',current_render_item_id=NULL WHERE status IN ('generating','waiting_for_production')")

    def create_run(self, run: dict[str, Any], items: Sequence[dict[str, Any]]) -> dict[str, Any]:
        now = utc_now()
        with self.transaction() as db:
            db.execute("""INSERT INTO modular_variant_runs(
                run_id,request_key,profile_id,profile_revision,profile_json,output_directory,created_at,status,
                requested_base_count,requested_variant_count,total_expected_outputs,rerun_of_run_id,
                transcript_bridge_version
            ) VALUES(?,?,?,?,?,?,?,'queued',?,?,?,?,?)""", (
                run["run_id"], run["request_key"], run["profile_id"], run["profile_revision"],
                json.dumps(run["profile"], ensure_ascii=False), run["output_directory"], now, len(items),
                run["requested_variant_count"], len(items) * run["requested_variant_count"], run.get("rerun_of_run_id"),
                run["transcript_bridge_version"],
            ))
            for item in items:
                db.execute("""INSERT INTO modular_variant_items(
                    run_id,render_run_id,modular_render_item_id,planner_run_id,composition_id,product,
                    renderer_version,base_path,base_identity,ordinal,variant_profile,transcript_words_json,
                    hook_text,output_directory,status,expected_variant_count,transcript_diagnostics_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'queued',?,?)""", (
                    run["run_id"], item["render_run_id"], item["modular_render_item_id"], item["planner_run_id"],
                    item["composition_id"], item["product"], item["renderer_version"], item["base_path"],
                    item["base_identity"], item["ordinal"], run["profile_id"],
                    json.dumps(item.get("transcript_words", []), ensure_ascii=False), item.get("hook_text", ""),
                    item["output_directory"], run["requested_variant_count"],
                    json.dumps(item.get("transcript_diagnostics", {}), ensure_ascii=False),
                ))
        return self.get_run(run["run_id"])

    def find_reusable(self, request_key: str) -> dict[str, Any] | None:
        with closing(self.connect()) as db:
            row = db.execute("""SELECT run_id FROM modular_variant_runs WHERE request_key=?
                AND status IN ('queued','waiting_for_production','generating','completed')
                ORDER BY CASE status WHEN 'completed' THEN 1 ELSE 0 END,created_at DESC LIMIT 1""", (request_key,)).fetchone()
        return self.get_run(str(row[0])) if row else None

    def pending_run_ids(self) -> list[str]:
        with closing(self.connect()) as db:
            rows = db.execute("SELECT run_id FROM modular_variant_runs WHERE status='queued' ORDER BY created_at").fetchall()
        return [str(row[0]) for row in rows]

    def set_run_state(self, run_id: str, status: str, item_id: str | None = None) -> None:
        with self.transaction() as db:
            db.execute("UPDATE modular_variant_runs SET status=?,current_render_item_id=? WHERE run_id=?", (status, item_id, run_id))

    def set_item_state(self, run_id: str, item_id: str, status: str) -> None:
        with self.transaction() as db:
            db.execute("UPDATE modular_variant_items SET status=?,started_at=COALESCE(started_at,?) WHERE run_id=? AND modular_render_item_id=?", (status, utc_now(), run_id, item_id))

    def replace_outputs(self, run_id: str, item_id: str, outputs: Sequence[dict[str, Any]]) -> None:
        with self.transaction() as db:
            db.execute("DELETE FROM modular_variant_outputs WHERE run_id=? AND modular_render_item_id=?", (run_id, item_id))
            for row in outputs:
                db.execute("""INSERT INTO modular_variant_outputs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    row["media_id"], run_id, item_id, row["variant_index"], row["variant_id"], row["variant_name"],
                    row["output_path"], row["duration"], row["width"], row["height"], int(row["has_video"]),
                    int(row["has_audio"]), row["file_size"], row["generation_seconds"], utc_now(),
                ))

    def complete_item(self, run_id: str, item_id: str, count: int, seconds: float) -> None:
        with self.transaction() as db:
            db.execute("""UPDATE modular_variant_items SET status='completed',produced_variant_count=?,error=NULL,
                generation_seconds=?,completed_at=? WHERE run_id=? AND modular_render_item_id=?""",
                (count, seconds, utc_now(), run_id, item_id))

    def fail_item(self, run_id: str, item_id: str, error: str, seconds: float) -> None:
        with self.transaction() as db:
            db.execute("""UPDATE modular_variant_items SET status='failed',error=?,generation_seconds=?,completed_at=?
                WHERE run_id=? AND modular_render_item_id=?""", (error[:2000], seconds, utc_now(), run_id, item_id))

    def finish_run(self, run_id: str) -> None:
        with self.transaction() as db:
            counts = db.execute("""SELECT COUNT(*) total,SUM(status='completed') succeeded,SUM(status='failed') failed,
                SUM(produced_variant_count) outputs FROM modular_variant_items WHERE run_id=?""", (run_id,)).fetchone()
            succeeded, failed = int(counts["succeeded"] or 0), int(counts["failed"] or 0)
            status = "completed" if failed == 0 else ("partial_failure" if succeeded else "failed")
            db.execute("""UPDATE modular_variant_runs SET status=?,succeeded_base_count=?,failed_base_count=?,
                total_completed_outputs=?,current_render_item_id=NULL,completed_at=? WHERE run_id=?""",
                (status, succeeded, failed, int(counts["outputs"] or 0), utc_now(), run_id))

    def item(self, run_id: str, item_id: str) -> dict[str, Any]:
        with closing(self.connect()) as db:
            row = db.execute("SELECT * FROM modular_variant_items WHERE run_id=? AND modular_render_item_id=?", (run_id, item_id)).fetchone()
        if row is None:
            raise KeyError("Unknown modular variant pilot item")
        result = dict(row)
        result["transcript_words"] = json.loads(result.pop("transcript_words_json"))
        result["transcript_diagnostics"] = json.loads(result.pop("transcript_diagnostics_json"))
        return result

    def get_run(self, run_id: str) -> dict[str, Any]:
        with closing(self.connect()) as db:
            row = db.execute("SELECT * FROM modular_variant_runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError("Unknown modular variant pilot run")
            items = [dict(item) for item in db.execute("SELECT * FROM modular_variant_items WHERE run_id=? ORDER BY ordinal", (run_id,))]
            outputs = [dict(item) for item in db.execute("SELECT * FROM modular_variant_outputs WHERE run_id=? ORDER BY modular_render_item_id,variant_index", (run_id,))]
        result = dict(row)
        result["profile"] = json.loads(result.pop("profile_json"))
        by_item: dict[str, list[dict[str, Any]]] = {}
        for output in outputs:
            output["has_video"] = bool(output["has_video"]); output["has_audio"] = bool(output["has_audio"])
            by_item.setdefault(str(output["modular_render_item_id"]), []).append(output)
        for item in items:
            item.pop("base_path", None); item.pop("transcript_words_json", None); item.pop("output_directory", None)
            item["transcript_diagnostics"] = json.loads(item.pop("transcript_diagnostics_json", "{}"))
            item["outputs"] = by_item.get(str(item["modular_render_item_id"]), [])
        result["items"] = items
        return result

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with closing(self.connect()) as db:
            rows = db.execute("SELECT run_id FROM modular_variant_runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [self.get_run(str(row[0])) for row in rows]

    def media_path(self, media_id: str) -> Path:
        with closing(self.connect()) as db:
            row = db.execute("SELECT output_path FROM modular_variant_outputs WHERE media_id=?", (media_id,)).fetchone()
        if row is None:
            raise KeyError("Unknown modular variant media")
        return Path(str(row[0])).resolve(strict=False)
