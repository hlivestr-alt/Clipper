from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = 2
TERMINAL_STATUSES = ("completed", "completed_with_failures", "failed", "cancelled")
ACTIVE_STATUSES = (
    "planning", "awaiting_review", "approved", "rendering_bases", "waiting_for_production",
    "generating_variants", "compliance", "scoring", "exporting", "cancelling",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class ModularProductionRepository:
    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
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
                INSERT INTO schema_meta(version) SELECT 1 WHERE NOT EXISTS(SELECT 1 FROM schema_meta);
                CREATE TABLE IF NOT EXISTS modular_production_jobs(
                    job_id TEXT PRIMARY KEY,
                    request_key TEXT NOT NULL,
                    workflow_mode TEXT NOT NULL CHECK(workflow_mode IN ('automatic','review_first')),
                    product TEXT NOT NULL,
                    requested_base_count INTEGER NOT NULL,
                    generated_base_count INTEGER NOT NULL DEFAULT 0,
                    rendered_base_count INTEGER NOT NULL DEFAULT 0,
                    failed_base_count INTEGER NOT NULL DEFAULT 0,
                    variants_per_base INTEGER NOT NULL,
                    expected_variant_count INTEGER NOT NULL DEFAULT 0,
                    generated_variant_count INTEGER NOT NULL DEFAULT 0,
                    failed_variant_count INTEGER NOT NULL DEFAULT 0,
                    compliance_passed_count INTEGER NOT NULL DEFAULT 0,
                    compliance_rejected_count INTEGER NOT NULL DEFAULT 0,
                    scored_count INTEGER NOT NULL DEFAULT 0,
                    scoring_failed_count INTEGER NOT NULL DEFAULT 0,
                    exported_count INTEGER NOT NULL DEFAULT 0,
                    export_failed_count INTEGER NOT NULL DEFAULT 0,
                    variant_profile_id TEXT NOT NULL,
                    variant_profile_revision TEXT NOT NULL,
                    variant_profile_json TEXT NOT NULL,
                    settings_json TEXT NOT NULL,
                    planner_run_id TEXT,
                    planner_manifest_id TEXT,
                    render_run_id TEXT,
                    modular_variant_run_id TEXT,
                    downstream_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL,
                    current_stage TEXT NOT NULL,
                    stage_progress REAL NOT NULL DEFAULT 0,
                    warnings_json TEXT NOT NULL DEFAULT '[]',
                    error_message TEXT,
                    output_directory TEXT NOT NULL,
                    working_directory TEXT NOT NULL,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    rerun_of_job_id TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    updated_at TEXT NOT NULL,
                    timings_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_modular_production_active_request
                    ON modular_production_jobs(request_key) WHERE status NOT IN ('completed','completed_with_failures','failed','cancelled');
                CREATE INDEX IF NOT EXISTS idx_modular_production_created
                    ON modular_production_jobs(created_at DESC);
                CREATE TABLE IF NOT EXISTS modular_production_items(
                    job_id TEXT NOT NULL REFERENCES modular_production_jobs(job_id) ON DELETE CASCADE,
                    composition_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    render_item_id TEXT,
                    render_status TEXT NOT NULL DEFAULT 'pending',
                    variant_status TEXT NOT NULL DEFAULT 'pending',
                    produced_variant_count INTEGER NOT NULL DEFAULT 0,
                    failed_variant_count INTEGER NOT NULL DEFAULT 0,
                    transcript_bridge_version TEXT,
                    transcript_diagnostics_json TEXT NOT NULL DEFAULT '{}',
                    base_identity TEXT,
                    error_message TEXT,
                    PRIMARY KEY(job_id,composition_id)
                );
                CREATE TABLE IF NOT EXISTS modular_production_variants(
                    job_id TEXT NOT NULL REFERENCES modular_production_jobs(job_id) ON DELETE CASCADE,
                    composition_id TEXT NOT NULL,
                    variant_index INTEGER NOT NULL,
                    media_id TEXT NOT NULL UNIQUE,
                    variant_id TEXT NOT NULL,
                    variant_name TEXT NOT NULL,
                    output_path TEXT NOT NULL,
                    status TEXT NOT NULL,
                    duration REAL,
                    file_size INTEGER,
                    lineage_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(job_id,composition_id,variant_index)
                );
                COMMIT;
                """
            )
            self._add_column(db, "modular_production_jobs", "product_scope", "TEXT NOT NULL DEFAULT 'single'")
            self._add_column(db, "modular_production_jobs", "product_allocation_json", "TEXT NOT NULL DEFAULT '{}'")
            self._add_column(db, "modular_production_jobs", "product_subflows_json", "TEXT NOT NULL DEFAULT '{}'")
            self._add_column(db, "modular_production_items", "product", "TEXT")
            self._add_column(db, "modular_production_items", "planner_run_id", "TEXT")
            self._add_column(db, "modular_production_items", "planner_manifest_id", "TEXT")
            self._add_column(db, "modular_production_items", "render_run_id", "TEXT")
            db.execute("UPDATE schema_meta SET version=?", (SCHEMA_VERSION,))
            db.commit()

    @staticmethod
    def _add_column(db: sqlite3.Connection, table: str, name: str, definition: str) -> None:
        columns = {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})")}
        if name not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def create_job(self, values: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        columns = (
            "job_id", "request_key", "workflow_mode", "product", "product_scope",
            "product_allocation_json", "product_subflows_json", "requested_base_count",
            "variants_per_base", "variant_profile_id", "variant_profile_revision", "variant_profile_json",
            "settings_json", "status", "current_stage", "output_directory", "working_directory",
            "rerun_of_job_id", "created_at", "started_at", "updated_at",
        )
        row = {**values, "created_at": now, "started_at": now, "updated_at": now}
        with self.transaction() as db:
            db.execute(
                f"INSERT INTO modular_production_jobs({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                tuple(self._encode(row.get(name)) for name in columns),
            )
        return self.get_job(values["job_id"], internal=True)

    def find_active(self, request_key: str) -> dict[str, Any] | None:
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT job_id FROM modular_production_jobs WHERE request_key=? AND status NOT IN (?,?,?,?) ORDER BY created_at DESC LIMIT 1",
                (request_key, *TERMINAL_STATUSES),
            ).fetchone()
        return self.get_job(row["job_id"], internal=True) if row else None

    def latest_matching(self, request_key: str) -> dict[str, Any] | None:
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT job_id FROM modular_production_jobs WHERE request_key=? ORDER BY created_at DESC LIMIT 1",
                (request_key,),
            ).fetchone()
        return self.get_job(row["job_id"], internal=True) if row else None

    def update_job(self, job_id: str, **values: Any) -> dict[str, Any]:
        if not values:
            return self.get_job(job_id, internal=True)
        allowed = {row[1] for row in self._table_info("modular_production_jobs")}
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"Unknown job fields: {sorted(unknown)}")
        values["updated_at"] = utc_now()
        assignments = ",".join(f"{name}=?" for name in values)
        with self.transaction() as db:
            cursor = db.execute(
                f"UPDATE modular_production_jobs SET {assignments} WHERE job_id=?",
                (*[self._encode(value) for value in values.values()], job_id),
            )
            if cursor.rowcount != 1:
                raise KeyError("Unknown modular production job")
        return self.get_job(job_id, internal=True)

    def _table_info(self, table: str) -> list[sqlite3.Row]:
        with closing(self.connect()) as db:
            return list(db.execute(f"PRAGMA table_info({table})"))

    def upsert_item(self, job_id: str, composition_id: str, **values: Any) -> None:
        defaults = {
            "ordinal": int(values.pop("ordinal", 0)), "render_item_id": None,
            "render_status": "pending", "variant_status": "pending", "produced_variant_count": 0,
            "failed_variant_count": 0, "transcript_bridge_version": None,
            "transcript_diagnostics_json": {}, "base_identity": None, "error_message": None,
            "product": None, "planner_run_id": None, "planner_manifest_id": None, "render_run_id": None,
        }
        defaults.update(values)
        columns = ["job_id", "composition_id", *defaults]
        update = ",".join(f"{name}=excluded.{name}" for name in defaults if name != "ordinal")
        with self.transaction() as db:
            db.execute(
                f"INSERT INTO modular_production_items({','.join(columns)}) VALUES({','.join('?' for _ in columns)}) "
                f"ON CONFLICT(job_id,composition_id) DO UPDATE SET {update}",
                (job_id, composition_id, *[self._encode(defaults[name]) for name in defaults]),
            )

    def update_item(self, job_id: str, composition_id: str, **values: Any) -> None:
        if not values:
            return
        assignments = ",".join(f"{name}=?" for name in values)
        with self.transaction() as db:
            db.execute(
                f"UPDATE modular_production_items SET {assignments} WHERE job_id=? AND composition_id=?",
                (*[self._encode(value) for value in values.values()], job_id, composition_id),
            )

    def upsert_variant(self, values: dict[str, Any]) -> None:
        row = {**values, "created_at": values.get("created_at") or utc_now()}
        columns = (
            "job_id", "composition_id", "variant_index", "media_id", "variant_id", "variant_name",
            "output_path", "status", "duration", "file_size", "lineage_json", "created_at",
        )
        with self.transaction() as db:
            db.execute(
                f"INSERT INTO modular_production_variants({','.join(columns)}) VALUES({','.join('?' for _ in columns)}) "
                "ON CONFLICT(job_id,composition_id,variant_index) DO UPDATE SET "
                "media_id=excluded.media_id,variant_id=excluded.variant_id,variant_name=excluded.variant_name,"
                "output_path=excluded.output_path,status=excluded.status,duration=excluded.duration,"
                "file_size=excluded.file_size,lineage_json=excluded.lineage_json",
                tuple(self._encode(row.get(name)) for name in columns),
            )

    def get_job(self, job_id: str, *, internal: bool = False) -> dict[str, Any]:
        with closing(self.connect()) as db:
            row = db.execute("SELECT * FROM modular_production_jobs WHERE job_id=?", (job_id,)).fetchone()
            if not row:
                raise KeyError("Unknown modular production job")
            items = list(db.execute(
                "SELECT * FROM modular_production_items WHERE job_id=? ORDER BY ordinal,composition_id", (job_id,)
            ))
            variants = list(db.execute(
                "SELECT * FROM modular_production_variants WHERE job_id=? ORDER BY composition_id,variant_index", (job_id,)
            ))
        payload = self._job_row(row)
        payload["items"] = [self._item_row(item) for item in items]
        by_composition: dict[str, list[dict[str, Any]]] = {}
        for item in variants:
            parsed = self._variant_row(item, internal=internal)
            by_composition.setdefault(parsed["composition_id"], []).append(parsed)
        for item in payload["items"]:
            item["variants"] = by_composition.get(item["composition_id"], [])
        if not internal:
            payload.pop("variant_profile", None)
            payload.pop("settings", None)
            payload.pop("output_directory", None)
            payload.pop("working_directory", None)
            for item in payload["items"]:
                item.pop("base_identity", None)
        return payload

    def list_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        with closing(self.connect()) as db:
            ids = [row[0] for row in db.execute(
                "SELECT job_id FROM modular_production_jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            )]
        return [self.get_job(job_id) for job_id in ids]

    def resumable_ids(self) -> list[str]:
        with closing(self.connect()) as db:
            return [row[0] for row in db.execute(
                "SELECT job_id FROM modular_production_jobs WHERE status NOT IN (?,?,?,?) ORDER BY created_at",
                TERMINAL_STATUSES,
            )]

    def media_path(self, media_id: str) -> Path:
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT output_path FROM modular_production_variants WHERE media_id=? AND status='completed'", (media_id,)
            ).fetchone()
        if not row:
            raise KeyError("Unknown modular production media")
        return Path(str(row[0])).resolve(strict=False)

    def cancel_remaining(self, job_id: str) -> None:
        """Mark work that will no longer be started while preserving completed artifacts."""
        with self.transaction() as db:
            db.execute(
                "UPDATE modular_production_items SET render_status='cancelled' "
                "WHERE job_id=? AND render_status IN ('pending','queued','waiting_for_production')",
                (job_id,),
            )
            db.execute(
                "UPDATE modular_production_items SET variant_status='cancelled' "
                "WHERE job_id=? AND variant_status IN ('pending','queued','waiting_for_production','generating')",
                (job_id,),
            )

    @staticmethod
    def _encode(value: Any) -> Any:
        if isinstance(value, (dict, list, tuple)):
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if isinstance(value, bool):
            return int(value)
        return value

    @staticmethod
    def _loads(value: Any, default: Any) -> Any:
        try:
            return json.loads(str(value))
        except (TypeError, json.JSONDecodeError):
            return default

    def _job_row(self, row: sqlite3.Row) -> dict[str, Any]:
        payload = dict(row)
        payload["variant_profile"] = self._loads(payload.pop("variant_profile_json"), {})
        payload["settings"] = self._loads(payload.pop("settings_json"), {})
        allocation = self._loads(payload.pop("product_allocation_json", "{}"), {})
        if not allocation:
            allocation = {payload["product"]: int(payload["requested_base_count"])}
        payload["product_allocation"] = allocation
        subflows = self._loads(payload.pop("product_subflows_json", "{}"), {})
        if not subflows:
            product = payload["product"]
            subflows = {product: {
                "product": product, "requested_base_count": int(payload["requested_base_count"]),
                "generated_base_count": int(payload["generated_base_count"]),
                "rendered_base_count": int(payload["rendered_base_count"]),
                "failed_base_count": int(payload["failed_base_count"]),
                "generated_variant_count": int(payload["generated_variant_count"]),
                "failed_variant_count": int(payload["failed_variant_count"]),
                "planner_run_id": payload.get("planner_run_id"),
                "planner_manifest_id": payload.get("planner_manifest_id"),
                "render_run_id": payload.get("render_run_id"), "status": payload["status"], "warnings": [],
            }}
        payload["product_subflows"] = subflows
        payload["downstream"] = self._loads(payload.pop("downstream_json"), {})
        payload["warnings"] = self._loads(payload.pop("warnings_json"), [])
        payload["timings"] = self._loads(payload.pop("timings_json"), {})
        payload["cancel_requested"] = bool(payload["cancel_requested"])
        return payload

    def _item_row(self, row: sqlite3.Row) -> dict[str, Any]:
        payload = dict(row)
        payload["transcript_diagnostics"] = self._loads(payload.pop("transcript_diagnostics_json"), {})
        return payload

    def _variant_row(self, row: sqlite3.Row, *, internal: bool) -> dict[str, Any]:
        payload = dict(row)
        payload["lineage"] = self._loads(payload.pop("lineage_json"), {})
        if internal:
            return payload
        payload.pop("output_path", None)
        payload["url"] = f"/api/modular-production/media/{payload['media_id']}"
        return payload
