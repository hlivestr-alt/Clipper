from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import zlib
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence
from uuid import uuid4


SCHEMA_VERSION = 1
DEFAULT_EVENT_RETENTION_SECONDS = 7 * 24 * 60 * 60
DEFAULT_EVENT_RETENTION_ROWS = 50_000


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def normalize_path(path: str | Path) -> tuple[str, str]:
    resolved = str(Path(path).expanduser().resolve())
    return os.path.normcase(resolved), resolved


def _compress_json(value: Any) -> bytes:
    return zlib.compress(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), 6)


def _decompress_json(value: bytes | None, fallback: str = "{}") -> Any:
    if value:
        return json.loads(zlib.decompress(value).decode("utf-8"))
    return json.loads(fallback)


@dataclass(frozen=True)
class ChangeEvent:
    sequence: int
    instance_id: str
    topics: tuple[str, ...]
    revisions: Mapping[str, int]
    occurred_at: str

    @property
    def event_id(self) -> str:
        return f"{self.instance_id}:{self.sequence}"


class CatalogDatabase:
    """Shared SQLite store for the rebuildable catalog, queue state, and events."""

    def __init__(self, path: str | Path, *, timeout: float = 5.0) -> None:
        self.path = Path(path).expanduser().resolve()
        self.timeout = max(0.1, float(timeout))
        self._migration_lock = threading.Lock()
        self._migrated = False

    @classmethod
    def from_config(cls, cfg: Any) -> "CatalogDatabase":
        override = os.getenv("CLIPPER_CATALOG_PATH", "").strip()
        working = Path(str(getattr(cfg, "WORKING_DIR", "working") or "working"))
        path = Path(override) if override else working / "catalog" / "clipper.sqlite3"
        return cls(path)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.path,
            timeout=self.timeout,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute(f"PRAGMA busy_timeout={int(self.timeout * 1000)}")
        return connection

    def ensure_schema(self) -> None:
        if self._migrated:
            return
        with self._migration_lock:
            if self._migrated:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = self.connect()
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.executescript(_SCHEMA_SQL)
                row = connection.execute("SELECT value FROM catalog_meta WHERE key='instance_id'").fetchone()
                if row is None:
                    connection.execute(
                        "INSERT INTO catalog_meta(key, value) VALUES('instance_id', ?)",
                        (uuid4().hex,),
                    )
                connection.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(?, ?)",
                    (SCHEMA_VERSION, utc_now()),
                )
                connection.commit()
                self._ensure_columns(connection)
            finally:
                connection.close()
            self._migrated = True

    @staticmethod
    def _ensure_columns(connection: sqlite3.Connection) -> None:
        additions = {
            "score_records": (("payload_blob", "BLOB"), ("ordinal", "INTEGER NOT NULL DEFAULT 0")),
            "compliance_results": (
                ("ordinal", "INTEGER NOT NULL DEFAULT 0"),
                ("detail_payload_json", "TEXT"),
            ),
            "compliance_violations": (("ordinal", "INTEGER NOT NULL DEFAULT 0"),),
            "modules": (("ordinal", "INTEGER NOT NULL DEFAULT 0"),),
        }
        for table, definitions in additions.items():
            columns = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
            for name, sql_type in definitions:
                if name not in columns:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")
        connection.commit()

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        self.ensure_schema()
        connection = self.connect()
        try:
            self._execute_retry(connection, "BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            self._execute_retry(connection, "COMMIT")
        except BaseException:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()

    @contextmanager
    def read_connection(self) -> Iterator[sqlite3.Connection]:
        self.ensure_schema()
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        self.ensure_schema()
        connection = self.connect()
        try:
            self._execute_retry(connection, sql, params)
            connection.commit()
        finally:
            connection.close()

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        self.ensure_schema()
        connection = self.connect()
        try:
            return list(self._execute_retry(connection, sql, params).fetchall())
        finally:
            connection.close()

    def scalar(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        rows = self.query(sql, params)
        return rows[0][0] if rows else default

    @staticmethod
    def _execute_retry(
        connection: sqlite3.Connection,
        sql: str,
        params: Sequence[Any] = (),
        *,
        attempts: int = 5,
    ) -> sqlite3.Cursor:
        for attempt in range(attempts):
            try:
                return connection.execute(sql, params)
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).casefold() or attempt + 1 >= attempts:
                    raise
                time.sleep(0.025 * (2**attempt))
        raise AssertionError("unreachable")

    def instance_id(self) -> str:
        return str(self.scalar("SELECT value FROM catalog_meta WHERE key='instance_id'", default=""))

    def reset_instance(self) -> str:
        value = uuid4().hex
        self.execute(
            "INSERT INTO catalog_meta(key, value) VALUES('instance_id', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (value,),
        )
        return value

    def status(self) -> dict[str, Any]:
        self.ensure_schema()
        integrity = str(self.scalar("PRAGMA quick_check", default="unknown"))
        table_counts = {
            table: int(self.scalar(f"SELECT COUNT(*) FROM {table}", default=0) or 0)
            for table in (
                "catalog_sources",
                "output_runs",
                "score_records",
                "compliance_results",
                "modules",
                "queue_active_runs",
                "queue_run_history",
                "change_events",
                "catalog_repairs",
            )
        }
        wal_path = Path(f"{self.path}-wal")
        shadow_rows = self.query("SELECT payload_json FROM catalog_snapshots WHERE key='shadow_comparison'")
        shadow = json.loads(shadow_rows[0]["payload_json"]) if shadow_rows else {
            "checked": False,
            "mismatch_count": 0,
            "domains": {},
        }
        return {
            "mode": os.getenv("CLIPPER_CATALOG_MODE", "legacy").strip().casefold() or "legacy",
            "schema_version": SCHEMA_VERSION,
            "instance_id": self.instance_id(),
            "database_path": str(self.path),
            "database_size": self.path.stat().st_size if self.path.exists() else 0,
            "wal_size": wal_path.stat().st_size if wal_path.exists() else 0,
            "integrity": integrity,
            "table_counts": table_counts,
            "dirty_source_count": table_counts["catalog_repairs"],
            "shadow_comparison": shadow,
            "revisions": {
                str(row["domain"]): int(row["revision"])
                for row in self.query("SELECT domain, revision FROM catalog_revisions ORDER BY domain")
            },
        }


class ChangeEventRepository:
    def __init__(self, database: CatalogDatabase) -> None:
        self.database = database

    def publish(self, topics: Iterable[str]) -> ChangeEvent:
        normalized = tuple(sorted({str(topic).strip().casefold() for topic in topics if str(topic).strip()}))
        if not normalized:
            normalized = ("*",)
        occurred_at = utc_now()
        with self.database.transaction(immediate=True) as connection:
            revisions: dict[str, int] = {}
            for topic in normalized:
                connection.execute(
                    "INSERT INTO catalog_revisions(domain, revision, updated_at) VALUES(?, 1, ?) "
                    "ON CONFLICT(domain) DO UPDATE SET revision=revision+1, updated_at=excluded.updated_at",
                    (topic, occurred_at),
                )
                row = connection.execute(
                    "SELECT revision FROM catalog_revisions WHERE domain=?", (topic,)
                ).fetchone()
                revisions[topic] = int(row[0])
            instance_id = str(
                connection.execute("SELECT value FROM catalog_meta WHERE key='instance_id'").fetchone()[0]
            )
            cursor = connection.execute(
                "INSERT INTO change_events(instance_id, topics_json, revisions_json, occurred_at) "
                "VALUES(?, ?, ?, ?)",
                (instance_id, json.dumps(normalized), json.dumps(revisions, sort_keys=True), occurred_at),
            )
            sequence = int(cursor.lastrowid)
        self.prune()
        return ChangeEvent(sequence, instance_id, normalized, revisions, occurred_at)

    def after(self, event_id: str | None, *, limit: int = 256) -> tuple[bool, list[ChangeEvent]]:
        instance_id = self.database.instance_id()
        sequence = 0
        if event_id:
            supplied_instance, separator, supplied_sequence = event_id.rpartition(":")
            if not separator or supplied_instance != instance_id:
                return True, []
            try:
                sequence = max(0, int(supplied_sequence))
            except ValueError:
                return True, []
        oldest = int(
            self.database.scalar(
                "SELECT COALESCE(MIN(sequence), 0) FROM change_events WHERE instance_id=?",
                (instance_id,),
                default=0,
            )
            or 0
        )
        if sequence and oldest and sequence < oldest - 1:
            return True, []
        rows = self.database.query(
            "SELECT sequence, instance_id, topics_json, revisions_json, occurred_at "
            "FROM change_events WHERE instance_id=? AND sequence>? ORDER BY sequence LIMIT ?",
            (instance_id, sequence, max(1, min(1000, int(limit)))),
        )
        return False, [
            ChangeEvent(
                int(row["sequence"]),
                str(row["instance_id"]),
                tuple(json.loads(row["topics_json"])),
                json.loads(row["revisions_json"]),
                str(row["occurred_at"]),
            )
            for row in rows
        ]

    def prune(self) -> None:
        cutoff = datetime.fromtimestamp(
            time.time() - DEFAULT_EVENT_RETENTION_SECONDS, timezone.utc
        ).isoformat(timespec="milliseconds")
        with self.database.transaction(immediate=True) as connection:
            connection.execute("DELETE FROM change_events WHERE occurred_at < ?", (cutoff,))
            count = int(connection.execute("SELECT COUNT(*) FROM change_events").fetchone()[0])
            excess = count - DEFAULT_EVENT_RETENTION_ROWS
            if excess > 0:
                connection.execute(
                    "DELETE FROM change_events WHERE sequence IN "
                    "(SELECT sequence FROM change_events ORDER BY sequence LIMIT ?)",
                    (excess,),
                )


class CatalogIndexer:
    """Idempotent artifact scanner used by shadow/backfill/reconcile workflows."""

    def __init__(self, database: CatalogDatabase, cfg: Any) -> None:
        self.database = database
        self.cfg = cfg

    def backfill(self, *, force: bool = False) -> dict[str, int]:
        counts = {"sources": 0, "outputs": 0, "modules": 0, "errors": 0}
        roots = (
            ("outputs", Path(str(getattr(self.cfg, "OUTPUT_DIR", "D:/output_clips")))),
            ("modules", Path(str(getattr(self.cfg, "MODULE_LIBRARY_DIR", "D:/proya_modules")))),
        )
        seen: set[str] = set()
        existing = {
            str(row["path_identity"]): row
            for row in self.database.query(
                "SELECT path_identity, mtime_ns, size, sha256 FROM catalog_sources"
            )
        }
        source_rows: list[tuple[Any, ...]] = []
        changed_manifests: list[Path] = []
        changed = False
        for domain, root in roots:
            if not root.exists():
                continue
            patterns = ("manifest.json", "scores_summary.json", "compliance*.json") if domain == "outputs" else ("index.json",)
            for pattern in patterns:
                for path in root.rglob(pattern):
                    identity, display = normalize_path(path)
                    if identity in seen:
                        continue
                    seen.add(identity)
                    try:
                        stat = path.stat()
                        previous = existing.get(identity)
                        if (
                            previous is not None
                            and int(previous["mtime_ns"]) == stat.st_mtime_ns
                            and int(previous["size"]) == stat.st_size
                        ):
                            counts["sources"] += 1
                            continue
                        digest = _file_digest(path)
                        source_rows.append(
                            (identity, display, domain, stat.st_mtime_ns, stat.st_size, digest, utc_now())
                        )
                        if domain == "outputs" and path.name.casefold() == "manifest.json":
                            changed_manifests.append(path)
                        changed = True
                        counts["sources"] += 1
                    except Exception as exc:
                        counts["errors"] += 1
                        self.record_repair(domain, display, exc)
        with self.database.transaction(immediate=True) as connection:
            connection.executemany(
                "INSERT INTO catalog_sources(path_identity, display_path, domain, mtime_ns, size, sha256, indexed_at, error) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, NULL) ON CONFLICT(path_identity) DO UPDATE SET "
                "display_path=excluded.display_path, domain=excluded.domain, mtime_ns=excluded.mtime_ns, "
                "size=excluded.size, sha256=excluded.sha256, indexed_at=excluded.indexed_at, error=NULL",
                source_rows,
            )
            connection.executemany(
                "DELETE FROM catalog_repairs WHERE domain=? AND source_path=?",
                [(row[2], row[1]) for row in source_rows],
            )
            missing = [(identity,) for identity in existing if identity not in seen]
            if missing:
                connection.executemany("DELETE FROM catalog_sources WHERE path_identity=?", missing)
                changed = True
        if changed_manifests:
            with self.database.transaction(immediate=True) as connection:
                for manifest in changed_manifests:
                    self._project_manifest_into(connection, manifest)
        for row in self.database.query("SELECT output_id, display_path FROM output_runs"):
            display = Path(str(row["display_path"]))
            if display.name == ".catalog":
                continue
            if not (display / "manifest.json").exists():
                self.database.execute("DELETE FROM output_runs WHERE output_id=?", (row["output_id"],))
        snapshot_ready = bool(
            self.database.scalar(
                "SELECT COUNT(*) FROM catalog_snapshots WHERE key='shadow_comparison'", default=0
            )
        )
        if changed or force or not snapshot_ready:
            self._project_read_models()
        if counts["errors"] == 0:
            self.database.execute(
                "DELETE FROM catalog_repairs WHERE source_path='post-mutation projection'"
            )
        counts["outputs"] = int(self.database.scalar("SELECT COUNT(*) FROM output_runs", default=0) or 0)
        counts["modules"] = int(self.database.scalar("SELECT COUNT(*) FROM modules", default=0) or 0)
        if changed or force or not snapshot_ready:
            ChangeEventRepository(self.database).publish(("outputs", "scores", "compliance", "modules"))
        return counts

    def verify(self) -> dict[str, Any]:
        missing: list[str] = []
        changed: list[str] = []
        for row in self.database.query(
            "SELECT path_identity, display_path, mtime_ns, size FROM catalog_sources ORDER BY display_path"
        ):
            path = Path(str(row["display_path"]))
            if not path.exists():
                missing.append(str(path))
                continue
            stat = path.stat()
            if stat.st_mtime_ns != int(row["mtime_ns"]) or stat.st_size != int(row["size"]):
                changed.append(str(path))
        return {"ok": not missing and not changed, "missing": missing, "changed": changed}

    def record_repair(self, domain: str, source_path: str, error: BaseException) -> None:
        self.database.execute(
            "INSERT INTO catalog_repairs(domain, source_path, error, attempts, created_at, updated_at) "
            "VALUES(?, ?, ?, 1, ?, ?) ON CONFLICT(domain, source_path) DO UPDATE SET "
            "error=excluded.error, attempts=catalog_repairs.attempts+1, updated_at=excluded.updated_at",
            (domain, source_path, str(error)[:2000], utc_now(), utc_now()),
        )

    def _project_file(self, domain: str, path: Path) -> None:
        if domain == "modules" and path.name.casefold() == "index.json":
            self._project_modules(path)
            return
        if path.name.casefold() == "manifest.json":
            self._project_manifest(path)
        elif path.name.casefold() == "scores_summary.json":
            self._project_scores(path)
        elif "compliance" in path.name.casefold():
            self._project_compliance(path)

    @staticmethod
    def _output_identity(output_dir: Path) -> tuple[str, str, str]:
        identity, display = normalize_path(output_dir)
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32], identity, display

    def _ensure_output(self, connection: sqlite3.Connection, output_dir: Path, payload: Any = None) -> str:
        output_id, identity, display = self._output_identity(output_dir)
        connection.execute(
            "INSERT INTO output_runs(output_id, path_identity, display_path, manifest_json) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(output_id) DO UPDATE SET display_path=excluded.display_path, manifest_json=CASE "
            "WHEN excluded.manifest_json='{}' THEN output_runs.manifest_json ELSE excluded.manifest_json END",
            (
                output_id,
                identity,
                display,
                json.dumps(
                    {"clip_count": len(payload), "indexed_at": utc_now()}
                    if isinstance(payload, list)
                    else {"indexed_at": utc_now()} if payload is not None else {},
                    ensure_ascii=False,
                ),
            ),
        )
        return output_id

    def _project_manifest(self, path: Path) -> None:
        payload = json.loads(path.read_text(encoding="utf-8"))
        with self.database.transaction(immediate=True) as connection:
            self._project_manifest_into(connection, path, payload)

    def _project_manifest_into(
        self,
        connection: sqlite3.Connection,
        path: Path,
        payload: Any | None = None,
    ) -> None:
        payload = json.loads(path.read_text(encoding="utf-8")) if payload is None else payload
        clips = payload if isinstance(payload, list) else list(payload.get("clips") or [])
        output_id = self._ensure_output(connection, path.parent, payload)
        connection.execute("DELETE FROM clips WHERE output_id=?", (output_id,))
        for index, clip in enumerate(clips):
                if not isinstance(clip, Mapping):
                    continue
                clip_name = str(clip.get("clip_id") or f"clip_{index:06d}")
                clip_id = f"{output_id}:{clip_name}"
                compact = {
                    key: clip.get(key)
                    for key in (
                        "clip_id", "product", "status", "output_file", "score", "duration",
                        "compliance_passed", "compliance_blocked", "violation_count", "auto_fixed",
                        "compliance_file", "scorer_total_score", "export_packaged_at",
                    )
                    if key in clip
                }
                connection.execute(
                    "INSERT INTO clips(clip_id, output_id, product, status, output_file, payload_json) "
                    "VALUES(?, ?, ?, ?, ?, ?)",
                    (
                        clip_id,
                        output_id,
                        str(clip.get("product") or ""),
                        str(clip.get("status") or ""),
                        str(clip.get("output_file") or ""),
                        json.dumps(compact, ensure_ascii=False),
                    ),
                )

    def _project_scores(self, path: Path) -> None:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            return
        groups = list(payload.get("groups") or payload.get("scores") or [])
        with self.database.transaction(immediate=True) as connection:
            output_id = self._ensure_output(connection, path.parent)
            connection.execute("DELETE FROM score_records WHERE output_id=?", (output_id,))
            connection.execute(
                "INSERT INTO score_summaries(output_id, scored_at, stats_json) VALUES(?, ?, ?) "
                "ON CONFLICT(output_id) DO UPDATE SET scored_at=excluded.scored_at, stats_json=excluded.stats_json",
                (
                    output_id,
                    str(payload.get("updated_at") or ""),
                    json.dumps(payload.get("scoring_optimization") or {}, ensure_ascii=False),
                ),
            )
            for index, score in enumerate(groups):
                if not isinstance(score, Mapping):
                    continue
                clip_id = str(score.get("clip_id") or score.get("base_clip_id") or index)
                variant_id = str(score.get("variant_id") or score.get("version") or "base")
                score_key = f"{output_id}:{clip_id}:{variant_id}"
                connection.execute(
                    "INSERT INTO score_records(score_key, output_id, base_score_key, product, status, total_score, scored_at, payload_json, payload_blob, ordinal) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        score_key,
                        output_id,
                        f"{output_id}:{str(score.get('base_clip_id') or clip_id)}",
                        str(score.get("product") or "general"),
                        str(score.get("status") or ""),
                        score.get("total_score"),
                        str(score.get("scored_at") or payload.get("updated_at") or ""),
                        json.dumps(score, ensure_ascii=False),
                        None,
                        index,
                    ),
                )

    def _project_compliance(self, path: Path) -> None:
        payload = json.loads(path.read_text(encoding="utf-8"))
        records = payload if isinstance(payload, list) else [payload]
        with self.database.transaction(immediate=True) as connection:
            output_id = self._ensure_output(connection, path.parent.parent if path.parent.name == "compliance" else path.parent)
            for index, record in enumerate(records):
                if not isinstance(record, Mapping):
                    continue
                natural = str(record.get("clip_id") or path.stem or index)
                result_id = f"{output_id}:{natural}"
                connection.execute(
                    "INSERT INTO compliance_results(result_id, output_id, product, status, passed, blocked, auto_fixed, checked_at, payload_json, ordinal) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(result_id) DO UPDATE SET "
                    "product=excluded.product, status=excluded.status, passed=excluded.passed, blocked=excluded.blocked, "
                    "auto_fixed=excluded.auto_fixed, checked_at=excluded.checked_at, payload_json=excluded.payload_json",
                    (
                        result_id,
                        output_id,
                        str(record.get("product") or "general"),
                        str(record.get("status") or ""),
                        int(bool(record.get("passed"))),
                        int(bool(record.get("blocked") or record.get("compliance_blocked"))),
                        int(bool(record.get("auto_fixed"))),
                        str(record.get("checked_at") or record.get("updated_at") or ""),
                        json.dumps(record, ensure_ascii=False),
                        index,
                    ),
                )

    def _project_modules(self, path: Path) -> None:
        payload = json.loads(path.read_text(encoding="utf-8"))
        modules = list(payload.get("modules") or []) if isinstance(payload, Mapping) else []
        with self.database.transaction(immediate=True) as connection:
            connection.execute("DELETE FROM modules")
            for index, module in enumerate(modules):
                if not isinstance(module, Mapping):
                    continue
                module_id = str(module.get("module_id") or index)
                connection.execute(
                    "INSERT INTO modules(module_id, product, role, review_status, source_date, payload_json, ordinal) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?)",
                    (
                        module_id,
                        str(module.get("product") or ""),
                        str(module.get("role") or ""),
                        str(module.get("review_status") or ""),
                        str(module.get("source_date") or ""),
                        json.dumps(module, ensure_ascii=False),
                        index,
                    ),
                )

    def _project_read_models(self) -> None:
        """Materialize the current API models once, outside request handling."""
        from clipper_app.application.read_services import ReadDashboardService
        from clipper_app.application.settings import LegacyConfigProvider

        reader = ReadDashboardService(LegacyConfigProvider(self.cfg), force_legacy=True)
        score_records, _signatures, score_warnings, score_stats = reader._score_records()
        compliance_rows, violations, _signatures, compliance_warnings = reader._compliance_records()
        compliance_detail_rows: dict[tuple[str, str], Any] = {}
        compliance_detail_violations: list[Any] = []
        for output_dir in dict.fromkeys(row.output_dir for row in compliance_rows if row.output_dir):
            detail_rows, detail_violations, _detail_signatures, detail_warnings = reader._compliance_records((output_dir,))
            compliance_warnings.extend(detail_warnings)
            compliance_detail_violations.extend(detail_violations)
            for detail_row in detail_rows:
                compliance_detail_rows[(detail_row.output_dir, detail_row.clip_id)] = detail_row
        module_rows, modules_by_id, _signature, module_warnings = reader._module_corpus()
        module_readiness = reader.module_readiness().data
        with self.database.transaction(immediate=True) as connection:
            synthetic_output = self._ensure_output(connection, Path(str(getattr(self.cfg, "OUTPUT_DIR", "D:/output_clips"))) / ".catalog")
            connection.execute("DELETE FROM score_records")
            for index, record in enumerate(score_records):
                row = record.row.model_dump(mode="json")
                storage_key = f"{row['score_key']}:{index:08d}"
                connection.execute(
                    "INSERT INTO score_records(score_key, output_id, base_score_key, product, status, total_score, scored_at, payload_json, payload_blob, ordinal) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        storage_key, synthetic_output, row["base_score_key"], row["product"], row["status"],
                        row.get("total_score"), row.get("scored_at") or row.get("sort_timestamp") or "",
                        json.dumps(row, ensure_ascii=False),
                        _compress_json({"raw": record.raw, "base_raw": record.base_raw}),
                        index,
                    ),
                )
            connection.execute("DELETE FROM compliance_violations")
            connection.execute("DELETE FROM compliance_results")
            for index, row_model in enumerate(compliance_rows):
                row = row_model.model_dump(mode="json")
                result_id = hashlib.sha256(
                    f"{row.get('output_dir')}|{row.get('clip_id')}|{row.get('checked_at')}".encode("utf-8")
                ).hexdigest()[:32]
                connection.execute(
                    "INSERT INTO compliance_results(result_id, output_id, product, status, passed, blocked, auto_fixed, checked_at, payload_json, ordinal, detail_payload_json) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        result_id, synthetic_output, row["product"], row["status"], int(row["passed"]),
                        int(row["blocked"]), int(row["auto_fixed"]), row["checked_at"], json.dumps(row, ensure_ascii=False), index,
                        json.dumps(
                            compliance_detail_rows.get((row["output_dir"], row["clip_id"]), row_model).model_dump(mode="json"),
                            ensure_ascii=False,
                        ),
                    ),
                )
            for index, violation_model in enumerate(compliance_detail_violations):
                row = violation_model.model_dump(mode="json")
                result_id_row = connection.execute(
                    "SELECT result_id FROM compliance_results WHERE json_extract(payload_json, '$.output_dir')=? "
                    "AND json_extract(payload_json, '$.clip_id')=? LIMIT 1",
                    (row.get("output_dir"), row.get("clip_id")),
                ).fetchone()
                if result_id_row is None:
                    continue
                violation_id = hashlib.sha256(
                    f"{result_id_row[0]}|{index}|{row.get('field')}|{row.get('original_text')}".encode("utf-8")
                ).hexdigest()[:32]
                connection.execute(
                    "INSERT INTO compliance_violations(violation_id, result_id, severity, violation_type, payload_json, ordinal) "
                    "VALUES(?, ?, ?, ?, ?, ?)",
                    (violation_id, result_id_row[0], row.get("severity"), row.get("violation_type"), json.dumps(row, ensure_ascii=False), index),
                )
            connection.execute("DELETE FROM modules")
            for index, row_model in enumerate(module_rows):
                row = row_model.model_dump(mode="json")
                raw = modules_by_id.get(row["module_id"], {})
                if raw:
                    row = reader._module_row(raw, include_artifact=True).model_dump(mode="json")
                connection.execute(
                    "INSERT INTO modules(module_id, product, role, review_status, source_date, payload_json, ordinal) VALUES(?, ?, ?, ?, ?, ?, ?)",
                    (
                        row["module_id"], row["product"], row["role"], row["review_status"], row["source_date"],
                        json.dumps({"row": row, "transcript_text": str(raw.get("transcript_text") or "")}, ensure_ascii=False),
                        index,
                    ),
                )
            snapshots = {
                "score_stats": score_stats.model_dump(mode="json"),
                "catalog_warnings": list(dict.fromkeys((*score_warnings, *compliance_warnings, *module_warnings))),
                "module_library_dir": str(getattr(self.cfg, "MODULE_LIBRARY_DIR", "")),
                "module_readiness": module_readiness.model_dump(mode="json"),
                "compliance_list_violations": [
                    violation.model_dump(mode="json") for violation in violations[:200]
                ],
            }
            for key, value in snapshots.items():
                connection.execute(
                    "INSERT INTO catalog_snapshots(key, payload_json, updated_at) VALUES(?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET payload_json=excluded.payload_json, updated_at=excluded.updated_at",
                    (key, json.dumps(value, ensure_ascii=False), utc_now()),
                )
        expected = {
            "scores": len(score_records),
            "compliance": len(compliance_rows),
            "modules": len(module_rows),
        }
        actual = {
            "scores": int(self.database.scalar("SELECT COUNT(*) FROM score_records", default=0) or 0),
            "compliance": int(self.database.scalar("SELECT COUNT(*) FROM compliance_results", default=0) or 0),
            "modules": int(self.database.scalar("SELECT COUNT(*) FROM modules", default=0) or 0),
        }
        comparison = {
            "checked": True,
            "checked_at": utc_now(),
            "mismatch_count": sum(expected[key] != actual[key] for key in expected),
            "domains": {
                key: {"legacy": expected[key], "catalog": actual[key], "matches": expected[key] == actual[key]}
                for key in expected
            },
        }
        self.database.execute(
            "INSERT INTO catalog_snapshots(key, payload_json, updated_at) VALUES('shadow_comparison', ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET payload_json=excluded.payload_json, updated_at=excluded.updated_at",
            (json.dumps(comparison, ensure_ascii=False), utc_now()),
        )


class CatalogQueryService:
    """SQL-backed implementations for the high-volume list/detail read APIs."""

    def __init__(self, database: CatalogDatabase, cfg: Any) -> None:
        self.database = database
        self.cfg = cfg

    def ready(self, table: str) -> bool:
        if self.database.scalar(f"SELECT COUNT(*) FROM {table}", default=0):
            return True
        rows = self.database.query(
            "SELECT payload_json FROM catalog_snapshots WHERE key='shadow_comparison'"
        )
        return bool(rows and json.loads(rows[0]["payload_json"]).get("checked"))

    def revision(self, *domains: str) -> str:
        values = {
            domain: int(
                self.database.scalar(
                    "SELECT revision FROM catalog_revisions WHERE domain=?", (domain,), default=0
                )
                or 0
            )
            for domain in domains
        }
        return json.dumps(values, sort_keys=True, separators=(",", ":"))

    def _snapshot(self, key: str, default: Any) -> Any:
        rows = self.database.query("SELECT payload_json FROM catalog_snapshots WHERE key=?", (key,))
        return json.loads(rows[0]["payload_json"]) if rows else default

    @staticmethod
    def _where(filters: Sequence[tuple[str, Any]]) -> tuple[str, list[Any]]:
        clauses = [clause for clause, value in filters if value not in (None, "")]
        params = [value for _clause, value in filters if value not in (None, "")]
        return (" WHERE " + " AND ".join(clauses) if clauses else ""), params

    def scores(
        self, *, limit: int, offset: int, search: str | None, status: str | None,
        product: str | None, sort: str, direction: str,
    ) -> Any:
        from clipper_app.contracts.read_models import ScoreIndexPage, ScoreRow, ScoreStats

        where, params = self._where((
            ("lower(payload_json) LIKE ?", f"%{search.casefold()}%" if search else None),
            ("lower(status)=?", status.casefold() if status else None),
            ("lower(product)=?", product.casefold() if product else None),
        ))
        columns = {
            "scored_at": "scored_at",
            "total_score": "total_score",
            "quality_score": "json_extract(payload_json, '$.quality_score')",
            "similarity_score": "json_extract(payload_json, '$.similarity_score')",
            "source_video": "json_extract(payload_json, '$.source_video')",
            "product": "product",
            "status": "status",
        }
        if sort not in columns:
            raise ValueError(f"Unsupported score sort: {sort}")
        order = columns[sort]
        direction_sql = "ASC" if direction.casefold() == "asc" else "DESC"
        total = int(self.database.scalar(f"SELECT COUNT(*) FROM score_records{where}", params, 0) or 0)
        records = self.database.query(
            f"SELECT payload_json FROM score_records{where} ORDER BY {order} {direction_sql}, ordinal ASC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        options = {
            "product": tuple(str(row[0]) for row in self.database.query("SELECT DISTINCT product FROM score_records WHERE product<>'' ORDER BY product")),
            "status": tuple(str(row[0]) for row in self.database.query("SELECT DISTINCT status FROM score_records WHERE status<>'' ORDER BY status")),
        }
        stats = ScoreStats.model_validate(self._snapshot("score_stats", {}))
        return ScoreIndexPage(
            rows=tuple(ScoreRow.model_validate(json.loads(row["payload_json"])) for row in records),
            total=total, limit=limit, offset=offset, stats=stats, filter_options=options,
        )

    def score_detail(self, score_key: str) -> Any:
        from clipper_app.contracts.read_models import ScoreDetail, ScoreRow

        rows = self.database.query(
            "SELECT payload_json, payload_blob, base_score_key FROM score_records "
            "WHERE json_extract(payload_json, '$.score_key')=? ORDER BY score_key LIMIT 1",
            (score_key,),
        )
        if not rows:
            return ScoreDetail()
        selected = json.loads(rows[0]["payload_json"])
        detail = _decompress_json(rows[0]["payload_blob"])
        variants = self.database.query(
            "SELECT payload_json FROM score_records WHERE base_score_key=? ORDER BY ordinal", (rows[0]["base_score_key"],)
        )
        return ScoreDetail(
            selected=ScoreRow.model_validate(selected),
            variants=tuple(ScoreRow.model_validate(json.loads(row["payload_json"])) for row in variants),
            raw=detail.get("raw") or {},
            base_raw=detail.get("base_raw") or {},
        )

    def overview(self, *, queue_active: bool | None, export_payload: Mapping[str, Any]) -> Any:
        from collections import defaultdict
        from clipper_app.contracts.read_models import (
            OverviewCompliance,
            OverviewExport,
            OverviewScoreTrendPoint,
            OverviewSummary,
            OverviewTopClip,
            ScoreRow,
        )

        with self.database.read_connection() as connection:
            score_stats = connection.execute(
                "SELECT COUNT(*) AS scored_count, AVG(total_score) AS average_score, "
                "COALESCE(SUM(CASE WHEN json_extract(payload_json, '$.compliance_blocked') THEN 1 ELSE 0 END),0) AS blocked "
                "FROM score_records"
            ).fetchone()
            trend_rows = connection.execute(
                "SELECT substr(scored_at,1,10) AS score_date, AVG(total_score) AS average_score, COUNT(*) AS scored_count "
                "FROM score_records WHERE total_score IS NOT NULL AND date(substr(scored_at,1,10)) >= date('now','-13 days') "
                "GROUP BY substr(scored_at,1,10) ORDER BY score_date"
            ).fetchall()
            top_rows = connection.execute(
                "SELECT payload_json FROM score_records WHERE total_score IS NOT NULL "
                "ORDER BY total_score DESC, scored_at DESC, ordinal ASC LIMIT 5"
            ).fetchall()
            compliance = connection.execute(
                "SELECT COUNT(*) AS scanned, COALESCE(SUM(passed),0) AS passed, COALESCE(SUM(blocked),0) AS blocked "
                "FROM compliance_results"
            ).fetchone()
            revisions = {
                str(row["domain"]): int(row["revision"])
                for row in connection.execute(
                    "SELECT domain, revision FROM catalog_revisions WHERE domain IN ('scores','compliance','outputs','queue')"
                ).fetchall()
            }
            if queue_active is None:
                queue_row = connection.execute(
                    "SELECT value_json FROM queue_meta WHERE key='state'"
                ).fetchone()
                queue_payload = json.loads(queue_row["value_json"]) if queue_row is not None else {}
                queue_active = str(queue_payload.get("queue_status") or "").casefold() in {
                    "running", "processing", "starting", "queued", "pausing", "stopping"
                }
        trend = tuple(
            OverviewScoreTrendPoint(
                date=str(row["score_date"]),
                average_score=round(float(row["average_score"]), 3),
                scored_count=int(row["scored_count"]),
            )
            for row in trend_rows
            if row["score_date"]
        )
        top = [
            ScoreRow.model_validate(json.loads(row["payload_json"]))
            for row in top_rows
        ]
        scanned = int(compliance["scanned"])
        passed = int(compliance["passed"])
        blocked = int(compliance["blocked"])
        scored_count = int(score_stats["scored_count"])
        if scanned == 0 and scored_count:
            scanned = scored_count
            blocked = int(score_stats["blocked"])
            passed = scanned - blocked

        def count(name: str) -> int:
            try:
                return max(0, int(export_payload.get(name) or 0))
            except (TypeError, ValueError):
                return 0

        actionable = count("actionable_count")
        packaged = count("packaged_count")
        pending = count("pending_count")
        available = export_payload.get("pending_count") is not None
        revision = json.dumps(
            {domain: revisions.get(domain, 0) for domain in ("scores", "compliance", "outputs", "queue")},
            sort_keys=True,
            separators=(",", ":"),
        )
        return OverviewSummary(
            revision=revision,
            queue_active=bool(queue_active),
            scored_count=scored_count,
            average_score=(
                round(float(score_stats["average_score"]), 3)
                if score_stats["average_score"] is not None else None
            ),
            export_ready_count=pending if available else 0,
            score_trend=trend,
            top_clips=tuple(
                OverviewTopClip(
                    score_key=row.score_key,
                    clip_id=row.clip_id,
                    product=row.product,
                    total_score=row.total_score,
                    scored_at=row.scored_at,
                    source_date=row.source_date,
                    artifact=row.artifact,
                )
                for row in top
            ),
            compliance=OverviewCompliance(
                scanned=scanned,
                passed=passed,
                blocked=blocked,
                rate=round((passed / scanned) * 100.0, 3) if scanned else 0.0,
            ),
            export=OverviewExport(
                available=available,
                actionable=actionable,
                ready=actionable,
                packaged_last_run=packaged,
                packaged=packaged,
                pending=pending,
                packaged_total=count("packaged_total"),
                error_count=count("error_count"),
                batch_size=count("batch_size"),
                progress=round((packaged / actionable) * 100) if actionable else 0,
                status=str(export_payload.get("status") or ""),
                updated_at=str(export_payload.get("updated_at") or ""),
                trigger=str(export_payload.get("trigger") or ""),
                dry_run=bool(export_payload.get("dry_run")),
            ),
        )

    def compliance(
        self, *, limit: int, offset: int, search: str | None, status: str | None,
        product: str | None, sort: str, direction: str, output_dir: str | None = None,
    ) -> Any:
        from clipper_app.contracts.read_models import ComplianceIndexPage, ComplianceRow, ComplianceViolationRow

        status_clause = None
        status_value: Any = None
        if status:
            key = status.casefold()
            if key in {"passed", "blocked", "auto_fixed"}:
                status_clause, status_value = f"{key}=?", 1
            else:
                status_clause, status_value = "lower(status)=?", key
        filters = [
            ("lower(payload_json) LIKE ?", f"%{search.casefold()}%" if search else None),
            ("lower(product)=?", product.casefold() if product else None),
            ("json_extract(payload_json, '$.output_dir')=?", output_dir),
        ]
        if status_clause:
            filters.append((status_clause, status_value))
        where, params = self._where(tuple(filters))
        columns = {
            "checked_at": "checked_at",
            "source_video": "json_extract(payload_json, '$.source_video')",
            "product": "product",
            "status": "status",
            "violation_count": "json_extract(payload_json, '$.violation_count')",
        }
        if sort not in columns:
            raise ValueError(f"Unsupported compliance sort: {sort}")
        order = columns[sort]
        direction_sql = "ASC" if direction.casefold() == "asc" else "DESC"
        total = int(self.database.scalar(f"SELECT COUNT(*) FROM compliance_results{where}", params, 0) or 0)
        rows = self.database.query(
            f"SELECT payload_json FROM compliance_results{where} ORDER BY {order} {direction_sql}, ordinal ASC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        all_matching = self.database.query(f"SELECT result_id, payload_json FROM compliance_results{where}", params)
        models = [ComplianceRow.model_validate(json.loads(row["payload_json"])) for row in all_matching]
        violations = self._snapshot("compliance_list_violations", [])
        options = {
            "product": tuple(str(row[0]) for row in self.database.query("SELECT DISTINCT product FROM compliance_results WHERE product<>'' ORDER BY product")),
            "status": tuple(name for name, present in (("passed", any(row.passed for row in models)), ("blocked", any(row.blocked for row in models)), ("auto_fixed", any(row.auto_fixed for row in models))) if present),
        }
        return ComplianceIndexPage(
            rows=tuple(ComplianceRow.model_validate(json.loads(row["payload_json"])) for row in rows),
            violations=tuple(ComplianceViolationRow.model_validate(row) for row in violations),
            total=total, limit=limit, offset=offset,
            summary={
                "scanned": len(models), "passed": sum(row.passed for row in models),
                "blocked": sum(row.blocked for row in models), "auto_fixed": sum(row.auto_fixed for row in models),
                "violation_count": sum(row.violation_count for row in models),
            },
            filter_options=options,
        )

    def compliance_detail(self, output_dir: str) -> Any:
        from clipper_app.contracts.read_models import ComplianceIndexPage, ComplianceRow, ComplianceViolationRow

        rows = self.database.query(
            "SELECT result_id, COALESCE(detail_payload_json, payload_json) AS payload_json FROM compliance_results "
            "WHERE json_extract(payload_json, '$.output_dir')=? ORDER BY ordinal",
            (output_dir,),
        )
        models = tuple(ComplianceRow.model_validate(json.loads(row["payload_json"])) for row in rows)
        result_ids = [str(row["result_id"]) for row in rows]
        violations = self.database.query(
            "SELECT payload_json FROM compliance_violations WHERE result_id IN ("
            + ",".join("?" for _ in result_ids)
            + ") ORDER BY ordinal",
            result_ids,
        ) if result_ids else []
        return ComplianceIndexPage(
            rows=models,
            violations=tuple(ComplianceViolationRow.model_validate(json.loads(row["payload_json"])) for row in violations),
            total=len(models),
            limit=max(1, len(models) or 1),
            offset=0,
            summary={
                "scanned": len(models),
                "passed": sum(row.passed for row in models),
                "blocked": sum(row.blocked for row in models),
                "auto_fixed": sum(row.auto_fixed for row in models),
                "violation_count": sum(row.violation_count for row in models),
            },
        )

    def modules(
        self, *, limit: int, offset: int, search: str | None, status: str | None, quality_status: str | None,
        review_status: str | None, visual_status: str | None, product: str | None,
        sort: str, direction: str,
    ) -> Any:
        from clipper_app.contracts.read_models import ModuleLibraryPage, ModuleLibraryRow

        where, params = self._where((
            ("lower(payload_json) LIKE ?", f"%{search.casefold()}%" if search else None),
            (
                "(lower(json_extract(payload_json, '$.row.quality_status'))=? OR lower(review_status)=? OR lower(json_extract(payload_json, '$.row.visual_validation_status'))=?)",
                (status.casefold(), status.casefold(), status.casefold()) if status else None,
            ),
            ("lower(json_extract(payload_json, '$.row.quality_status'))=?", quality_status.casefold() if quality_status else None),
            ("lower(review_status)=?", review_status.casefold() if review_status else None),
            ("lower(json_extract(payload_json, '$.row.visual_validation_status'))=?", visual_status.casefold() if visual_status else None),
            ("lower(product)=?", product.casefold() if product else None),
        ))
        columns = {
            "product": ("product", "source_date", "role", "module_id"),
            "role": ("role",),
            "source_date": ("source_date",),
            "review_status": ("review_status",),
            "confidence": ("json_extract(payload_json, '$.row.confidence')",),
            "duration": ("json_extract(payload_json, '$.row.duration')",),
            "status": (
                "json_extract(payload_json, '$.row.quality_status')",
                "review_status",
            ),
        }
        if sort not in columns:
            raise ValueError(f"Unsupported module sort: {sort}")
        direction_sql = "ASC" if direction.casefold() == "asc" else "DESC"
        order = ", ".join(f"{column} {direction_sql}" for column in columns[sort])
        flattened: list[Any] = []
        for value in params:
            flattened.extend(value if isinstance(value, tuple) else (value,))
        params = flattened
        total = int(self.database.scalar(f"SELECT COUNT(*) FROM modules{where}", params, 0) or 0)
        rows = self.database.query(
            f"SELECT payload_json FROM modules{where} ORDER BY {order}, ordinal ASC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        def distinct(path: str) -> tuple[str, ...]:
            return tuple(str(row[0]) for row in self.database.query(f"SELECT DISTINCT {path} FROM modules WHERE {path}<>'' ORDER BY {path}"))
        return ModuleLibraryPage(
            library_dir=str(self._snapshot("module_library_dir", getattr(self.cfg, "MODULE_LIBRARY_DIR", ""))),
            rows=tuple(ModuleLibraryRow.model_validate(json.loads(row["payload_json"])["row"]) for row in rows),
            total=total, limit=limit, offset=offset,
            filter_options={
                "product": distinct("product"), "source_date": distinct("source_date"),
                "quality_status": distinct("json_extract(payload_json, '$.row.quality_status')"),
                "visual_validation_status": distinct("json_extract(payload_json, '$.row.visual_validation_status')"),
                "review_status": distinct("review_status"),
            },
        )

    def module_detail(self, module_id: str) -> Any:
        from clipper_app.contracts.read_models import ModuleDetail, ModuleLibraryRow

        rows = self.database.query("SELECT payload_json FROM modules WHERE module_id=?", (module_id,))
        if not rows:
            return ModuleDetail()
        bundle = json.loads(rows[0]["payload_json"])
        return ModuleDetail(
            selected=ModuleLibraryRow.model_validate(bundle["row"]),
            transcript_text=str(bundle.get("transcript_text") or ""),
        )

    def module_readiness(self) -> Any:
        from clipper_app.contracts.read_models import ModuleReadiness

        return ModuleReadiness.model_validate(self._snapshot("module_readiness", {}))


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS catalog_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS catalog_sources (
    path_identity TEXT PRIMARY KEY,
    display_path TEXT NOT NULL,
    domain TEXT NOT NULL,
    mtime_ns INTEGER NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    indexed_at TEXT NOT NULL,
    error TEXT
);
CREATE INDEX IF NOT EXISTS catalog_sources_domain_idx ON catalog_sources(domain, indexed_at);
CREATE TABLE IF NOT EXISTS catalog_revisions (
    domain TEXT PRIMARY KEY,
    revision INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS catalog_snapshots (
    key TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS change_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id TEXT NOT NULL,
    topics_json TEXT NOT NULL,
    revisions_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS change_events_occurred_idx ON change_events(occurred_at);
CREATE TABLE IF NOT EXISTS catalog_repairs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain TEXT NOT NULL,
    source_path TEXT NOT NULL,
    error TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(domain, source_path)
);
CREATE TABLE IF NOT EXISTS output_runs (
    output_id TEXT PRIMARY KEY,
    path_identity TEXT NOT NULL UNIQUE,
    display_path TEXT NOT NULL,
    source_video TEXT,
    run_tag TEXT,
    status TEXT,
    created_at TEXT,
    completed_at TEXT,
    manifest_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS output_runs_completed_idx ON output_runs(completed_at DESC);
CREATE TABLE IF NOT EXISTS clips (
    clip_id TEXT PRIMARY KEY,
    output_id TEXT NOT NULL REFERENCES output_runs(output_id) ON DELETE CASCADE,
    product TEXT,
    status TEXT,
    output_file TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS clips_output_idx ON clips(output_id, product, status);
CREATE TABLE IF NOT EXISTS score_summaries (
    output_id TEXT PRIMARY KEY REFERENCES output_runs(output_id) ON DELETE CASCADE,
    scored_at TEXT,
    stats_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS score_records (
    score_key TEXT PRIMARY KEY,
    output_id TEXT NOT NULL REFERENCES output_runs(output_id) ON DELETE CASCADE,
    base_score_key TEXT,
    product TEXT,
    status TEXT,
    total_score REAL,
    scored_at TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    payload_blob BLOB,
    ordinal INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS score_records_query_idx ON score_records(product, status, scored_at DESC);
CREATE TABLE IF NOT EXISTS compliance_results (
    result_id TEXT PRIMARY KEY,
    output_id TEXT NOT NULL REFERENCES output_runs(output_id) ON DELETE CASCADE,
    product TEXT,
    status TEXT,
    passed INTEGER NOT NULL DEFAULT 0,
    blocked INTEGER NOT NULL DEFAULT 0,
    auto_fixed INTEGER NOT NULL DEFAULT 0,
    checked_at TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    ordinal INTEGER NOT NULL DEFAULT 0,
    detail_payload_json TEXT
);
CREATE INDEX IF NOT EXISTS compliance_query_idx ON compliance_results(product, status, checked_at DESC);
CREATE TABLE IF NOT EXISTS compliance_violations (
    violation_id TEXT PRIMARY KEY,
    result_id TEXT NOT NULL REFERENCES compliance_results(result_id) ON DELETE CASCADE,
    severity TEXT,
    violation_type TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    ordinal INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS modules (
    module_id TEXT PRIMARY KEY,
    product TEXT,
    role TEXT,
    review_status TEXT,
    source_date TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    ordinal INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS modules_query_idx ON modules(product, role, review_status, source_date DESC);
CREATE TABLE IF NOT EXISTS export_status (
    export_id TEXT PRIMARY KEY,
    updated_at TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS queue_meta (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS queue_active_runs (
    video_key TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS queue_active_stages (
    video_key TEXT NOT NULL REFERENCES queue_active_runs(video_key) ON DELETE CASCADE,
    stage TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY(video_key, stage)
);
CREATE TABLE IF NOT EXISTS queue_run_history (
    run_id TEXT PRIMARY KEY,
    video_key TEXT NOT NULL,
    archived_at TEXT NOT NULL,
    journal_segment TEXT NOT NULL,
    checksum TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS queue_history_video_idx ON queue_run_history(video_key, archived_at DESC);
CREATE TRIGGER IF NOT EXISTS queue_history_no_update BEFORE UPDATE ON queue_run_history
BEGIN SELECT RAISE(ABORT, 'queue history is immutable'); END;
CREATE TRIGGER IF NOT EXISTS queue_history_no_delete BEFORE DELETE ON queue_run_history
BEGIN SELECT RAISE(ABORT, 'queue history is immutable'); END;
"""
