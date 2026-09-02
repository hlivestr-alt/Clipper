from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from collections import defaultdict
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from .models import LifecycleClass
from .registry import ArtifactRegistry, canonical_json, stable_id, utc_now
from .transcripts import (
    RAW_CHECKPOINT_NAME,
    REFERENCE_NAME,
    TRANSCRIPT_NAME,
    TranscriptArtifactStore,
    file_sha256,
    resolve_effective_raw_checkpoint_path,
    resolve_effective_transcript_path,
    source_byte_identity,
)


MIGRATION_SCHEMA_VERSION = 1
SAFE_TRANSCRIPT = "MIGRATE_CANONICAL"
SAFE_RAW = "SAFE_DELETE_RAW"
SAFE_HARDLINK = "SAFE_HARDLINK_ALIAS"


@dataclass(frozen=True)
class TreeMetrics:
    bytes: int
    files: int


def measure_tree(root: str | Path) -> TreeMetrics:
    total = 0
    count = 0
    for base, _dirs, files in os.walk(Path(root)):
        for name in files:
            try:
                total += (Path(base) / name).stat().st_size
                count += 1
            except OSError:
                continue
    return TreeMetrics(total, count)


class MigrationJournal:
    """Durable, resumable evidence journal for Phase 2A."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve(strict=False)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "journal.sqlite3"
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        return db

    def _initialize(self) -> None:
        with closing(self.connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS migration_meta(
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS hash_cache(
                    path TEXT PRIMARY KEY,
                    size_bytes INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    confirmed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS candidates(
                    candidate_id TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    original_path TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    content_hash TEXT,
                    canonical_path TEXT,
                    classification TEXT NOT NULL,
                    proposed_action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    blocker TEXT,
                    evidence_json TEXT NOT NULL,
                    discovered_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_candidates_category ON candidates(category,classification,status);
                CREATE TABLE IF NOT EXISTS actions(
                    action_id TEXT PRIMARY KEY,
                    candidate_id TEXT REFERENCES candidates(candidate_id),
                    action TEXT NOT NULL,
                    state TEXT NOT NULL,
                    bytes_affected INTEGER NOT NULL DEFAULT 0,
                    before_path TEXT,
                    after_path TEXT,
                    evidence_json TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS backups(
                    backup_id TEXT PRIMARY KEY,
                    source_path TEXT NOT NULL,
                    backup_path TEXT NOT NULL,
                    source_size INTEGER NOT NULL,
                    backup_size INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    integrity_status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            db.commit()

    def set_meta(self, key: str, value: Any) -> None:
        with closing(self.connect()) as db:
            db.execute(
                "INSERT INTO migration_meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
                (key, canonical_json(value)),
            )
            db.commit()

    def get_meta(self, key: str, default: Any = None) -> Any:
        with closing(self.connect()) as db:
            row = db.execute("SELECT value_json FROM migration_meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def hash_file(self, path: str | Path) -> str:
        resolved = Path(path).resolve(strict=True)
        stat = resolved.stat()
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT sha256 FROM hash_cache WHERE path=? AND size_bytes=? AND mtime_ns=?",
                (str(resolved), stat.st_size, stat.st_mtime_ns),
            ).fetchone()
        if row:
            return str(row[0])
        digest = file_sha256(resolved)
        with closing(self.connect()) as db:
            db.execute(
                """INSERT INTO hash_cache VALUES(?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET
                   size_bytes=excluded.size_bytes,mtime_ns=excluded.mtime_ns,
                   sha256=excluded.sha256,confirmed_at=excluded.confirmed_at""",
                (str(resolved), stat.st_size, stat.st_mtime_ns, digest, utc_now()),
            )
            db.commit()
        return digest

    def upsert_candidate(self, row: dict[str, Any]) -> None:
        now = utc_now()
        with closing(self.connect()) as db:
            existing = db.execute(
                "SELECT status FROM candidates WHERE candidate_id=?", (row["candidate_id"],)
            ).fetchone()
            status = str(existing[0]) if existing and str(existing[0]) in {"COMPLETED", "PARTIAL"} else row.get("status", "PLANNED")
            db.execute(
                """INSERT INTO candidates VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(candidate_id) DO UPDATE SET
                   category=excluded.category,original_path=excluded.original_path,
                   size_bytes=excluded.size_bytes,content_hash=excluded.content_hash,
                   canonical_path=excluded.canonical_path,classification=excluded.classification,
                   proposed_action=excluded.proposed_action,blocker=excluded.blocker,
                   evidence_json=excluded.evidence_json,updated_at=excluded.updated_at""",
                (
                    row["candidate_id"], row["category"], row["original_path"], int(row.get("size_bytes") or 0),
                    row.get("content_hash"), row.get("canonical_path"), row["classification"],
                    row["proposed_action"], status, row.get("blocker"),
                    canonical_json(row.get("evidence") or {}), now, now,
                ),
            )
            db.commit()

    def candidate_rows(self, *, classification: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM candidates"
        params: tuple[Any, ...] = ()
        if classification:
            sql += " WHERE classification=?"
            params = (classification,)
        sql += " ORDER BY category,original_path"
        with closing(self.connect()) as db:
            rows = db.execute(sql, params).fetchall()
        result = []
        for raw in rows:
            row = dict(raw)
            row["evidence"] = json.loads(row.pop("evidence_json"))
            result.append(row)
        return result

    def set_candidate_status(self, candidate_id: str, status: str, *, blocker: str | None = None) -> None:
        with closing(self.connect()) as db:
            db.execute(
                "UPDATE candidates SET status=?,blocker=COALESCE(?,blocker),updated_at=? WHERE candidate_id=?",
                (status, blocker, utc_now(), candidate_id),
            )
            db.commit()

    def mark_nonexecuted_retained(self) -> None:
        with closing(self.connect()) as db:
            db.execute(
                """UPDATE candidates SET status='RETAINED',updated_at=?
                   WHERE status='PLANNED' AND classification NOT IN (?,?,?,?)""",
                (utc_now(), SAFE_TRANSCRIPT, SAFE_RAW, SAFE_HARDLINK, "SAFE_RETIRE_TEST_METADATA"),
            )
            db.commit()

    def record_action(
        self,
        candidate_id: str | None,
        action: str,
        state: str,
        *,
        bytes_affected: int = 0,
        before_path: str | Path | None = None,
        after_path: str | Path | None = None,
        evidence: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> str:
        action_id = uuid4().hex
        with closing(self.connect()) as db:
            db.execute(
                "INSERT INTO actions VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    action_id, candidate_id, action, state, int(bytes_affected),
                    str(before_path) if before_path else None, str(after_path) if after_path else None,
                    canonical_json(evidence or {}), error, utc_now(),
                ),
            )
            db.commit()
        return action_id

    def add_backup(self, row: dict[str, Any]) -> None:
        with closing(self.connect()) as db:
            db.execute(
                "INSERT OR REPLACE INTO backups VALUES(?,?,?,?,?,?,?,?)",
                (
                    row["backup_id"], row["source_path"], row["backup_path"], row["source_size"],
                    row["backup_size"], row["sha256"], row["integrity_status"], row["created_at"],
                ),
            )
            db.commit()

    def export(self, target: str | Path) -> dict[str, Any]:
        with closing(self.connect()) as db:
            meta = {row[0]: json.loads(row[1]) for row in db.execute("SELECT key,value_json FROM migration_meta")}
            backups = [dict(row) for row in db.execute("SELECT * FROM backups ORDER BY source_path")]
            actions = []
            for raw in db.execute("SELECT * FROM actions ORDER BY created_at,action_id"):
                row = dict(raw)
                row["evidence"] = json.loads(row.pop("evidence_json"))
                actions.append(row)
        candidates = self.candidate_rows()
        payload = {
            "schema_version": MIGRATION_SCHEMA_VERSION,
            "migration": meta,
            "backups": backups,
            "summary": _journal_summary(candidates, actions),
            "candidates": [_compact_candidate(row) for row in candidates],
            "actions": actions,
        }
        output = Path(target)
        output.parent.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(output, payload)
        return payload


class Phase2AMigrator:
    def __init__(self, project_root: str | Path, migration_id: str | None = None):
        self.project_root = Path(project_root).resolve()
        self.working = (self.project_root / "working").resolve()
        self.migration_id = migration_id or datetime.now(timezone.utc).strftime("phase2a_%Y%m%dT%H%M%SZ")
        self.migration_root = self.working / "storage_migrations" / self.migration_id
        self.journal = MigrationJournal(self.migration_root)
        self.registry = ArtifactRegistry.from_working_dir(self.working)
        self.transcripts = TranscriptArtifactStore(self.working, self.registry)
        self.report_manifest = self.project_root / "reports" / "storage-phase2a-manifest.json"
        self.journal.set_meta("migration_id", self.migration_id)
        self.journal.set_meta("schema_version", MIGRATION_SCHEMA_VERSION)
        self.journal.set_meta("project_root", str(self.project_root))
        self.journal.set_meta("working_root", str(self.working))
        self.journal.set_meta("created_at", self.journal.get_meta("created_at") or utc_now())

    def discover(self) -> dict[str, Any]:
        if self.journal.get_meta("execution_started"):
            raise RuntimeError("Cannot rediscover after execution has started; resume the existing journal")
        baseline = self.journal.get_meta("baseline") or {
            "project": asdict(measure_tree(self.project_root)),
            "working": asdict(measure_tree(self.working)),
        }
        self.journal.set_meta("baseline", baseline)
        self.journal.set_meta("roots_in_scope", {
            "destructive": [str(self.project_root), str(self.working)],
            "inventory_only": [r"D:\output_clips", r"D:\VOD"],
        })
        owners = _queue_owner_index(self.project_root, self.working)
        transcript_counts = self._discover_transcripts(owners)
        raw_counts = self._discover_raw_cuts(owners)
        broll_counts = self._discover_broll_aliases()
        render_analysis = self._discover_exact_render_groups()
        registry_test_metadata = self._discover_registry_test_metadata()
        secondary = self._secondary_inventory()
        self.journal.set_meta("discovery", {
            "transcripts": transcript_counts,
            "raw_cuts": raw_counts,
            "broll": broll_counts,
            "modular_renders": render_analysis,
            "registry_test_metadata": registry_test_metadata,
            "secondary": secondary,
        })
        self.journal.set_meta("plan_completed_at", utc_now())
        return self.journal.export(self.report_manifest)

    def create_backups(self) -> list[dict[str, Any]]:
        if self.journal.get_meta("backups_complete"):
            return self._verify_existing_backups()
        backup_root = self.migration_root / "backups"
        backup_root.mkdir(parents=True, exist_ok=True)
        self.journal.export(self.migration_root / "plan-manifest.json")
        if not self.journal.get_meta("pre_execution_snapshot"):
            self.journal.set_meta("pre_execution_snapshot", {
                "captured_at": utc_now(),
                "project": asdict(measure_tree(self.project_root)),
                "working": asdict(measure_tree(self.working)),
                "volume_free_bytes": shutil.disk_usage(self.project_root).free,
                "planned_reclamation": _planned_reclamation(self.journal),
            })
        rows: list[dict[str, Any]] = []
        db_paths = [
            self.working / "catalog" / "clipper.sqlite3",
            self.working / "modular_library.sqlite3",
            self.working / "modular_planner.sqlite3",
            self.working / "modular_production.sqlite3",
            self.working / "modular_renderer.sqlite3",
            self.working / "modular_variant_pilot.sqlite3",
            self.working / "artifacts" / "artifact_registry.sqlite3",
        ]
        for source in db_paths:
            if not source.is_file():
                continue
            target = backup_root / f"{source.parent.name}_{source.name}"
            source_uri = f"file:{source.resolve().as_posix()}?mode=ro"
            with closing(sqlite3.connect(source_uri, uri=True)) as src, closing(sqlite3.connect(target)) as dst:
                source_integrity = str(src.execute("PRAGMA integrity_check").fetchone()[0])
                if source_integrity != "ok":
                    raise RuntimeError(f"Source database integrity failed for {source}: {source_integrity}")
                src.backup(dst)
            with closing(sqlite3.connect(f"file:{target.resolve().as_posix()}?mode=ro", uri=True)) as check:
                integrity = str(check.execute("PRAGMA integrity_check").fetchone()[0])
            if integrity != "ok":
                raise RuntimeError(f"Backup integrity failed for {source}: {integrity}")
            row = _backup_row(source, target, f"source-{source_integrity};backup-{integrity}")
            self.journal.add_backup(row)
            rows.append(row)

        metadata_zip = backup_root / "metadata.zip"
        metadata_sources = list(_metadata_backup_sources(self.project_root, self.working, self.journal))
        with zipfile.ZipFile(metadata_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for source in metadata_sources:
                try:
                    archive_name = source.relative_to(self.project_root).as_posix()
                except ValueError:
                    source_key = hashlib.sha256(str(source).encode()).hexdigest()[:16]
                    archive_name = f"external_manifests/{source_key}_{source.name}"
                archive.write(source, archive_name)
        with zipfile.ZipFile(metadata_zip, "r") as archive:
            bad = archive.testzip()
        if bad:
            raise RuntimeError(f"Metadata backup failed verification at {bad}")
        source_bytes = sum(path.stat().st_size for path in metadata_sources)
        row = {
            "backup_id": stable_id("backup", str(metadata_zip)),
            "source_path": "metadata-set",
            "backup_path": str(metadata_zip),
            "source_size": source_bytes,
            "backup_size": metadata_zip.stat().st_size,
            "sha256": file_sha256(metadata_zip),
            "integrity_status": "zip-ok",
            "created_at": utc_now(),
        }
        self.journal.add_backup(row)
        rows.append(row)
        self.journal.set_meta("backups_complete", True)
        self.journal.set_meta("backups_completed_at", utc_now())
        self.journal.export(self.report_manifest)
        return rows

    def _verify_existing_backups(self) -> list[dict[str, Any]]:
        payload = self.journal.export(self.report_manifest)
        rows = payload["backups"]
        if not rows:
            raise RuntimeError("Backup completion marker exists without backup records")
        for row in rows:
            target = Path(row["backup_path"])
            if not target.is_file() or target.stat().st_size != int(row["backup_size"]):
                raise RuntimeError(f"Backup missing or size changed: {target}")
            if file_sha256(target) != row["sha256"]:
                raise RuntimeError(f"Backup checksum changed: {target}")
            if target.suffix.casefold() == ".zip":
                with zipfile.ZipFile(target, "r") as archive:
                    if archive.testzip():
                        raise RuntimeError(f"Backup ZIP integrity failed: {target}")
            elif target.suffix.casefold() == ".sqlite3":
                with closing(sqlite3.connect(f"file:{target.resolve().as_posix()}?mode=ro", uri=True)) as db:
                    if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise RuntimeError(f"Backup database integrity failed: {target}")
        self.journal.set_meta("backups_last_verified_at", utc_now())
        return rows

    def apply(self) -> dict[str, Any]:
        if not self.journal.get_meta("plan_completed_at"):
            raise RuntimeError("A completed discovery plan is required before execution")
        self._assert_application_idle()
        self.create_backups()
        if not self.journal.get_meta("backups_complete"):
            raise RuntimeError("Verified backups are required before execution")
        self.journal.set_meta("execution_started", True)
        self.journal.set_meta("execution_started_at", self.journal.get_meta("execution_started_at") or utc_now())
        self._apply_transcripts()
        self._apply_raw_cuts()
        self._apply_broll_aliases()
        self._apply_registry_test_metadata()
        after = {
            "project": asdict(measure_tree(self.project_root)),
            "working": asdict(measure_tree(self.working)),
        }
        self.journal.set_meta("after", after)
        self.journal.set_meta("execution_completed_at", utc_now())
        return self.journal.export(self.report_manifest)

    def inventory_external(self) -> dict[str, Any]:
        """Metadata-only external inventory. This method has no mutation path."""
        output = _external_output_inventory(Path(r"D:\output_clips"))
        vod = _external_vod_inventory(Path(r"D:\VOD"))
        payload = {
            "generated_at": utc_now(),
            "deletion_performed": False,
            "output_clips": output,
            "vod": vod,
        }
        self.journal.set_meta("external_inventory", payload)
        return self.journal.export(self.report_manifest)

    def finalize_project_inventory(self) -> dict[str, Any]:
        self.journal.mark_nonexecuted_retained()
        metrics = _project_ownership_metrics(self.project_root, self.registry, self.journal)
        metrics["volume_free_bytes"] = shutil.disk_usage(self.project_root).free
        before = self.journal.get_meta("pre_execution_snapshot") or {}
        if before.get("volume_free_bytes") is not None:
            metrics["volume_free_delta_bytes"] = metrics["volume_free_bytes"] - int(before["volume_free_bytes"])
        self.journal.set_meta("final_project_inventory", metrics)
        return self.journal.export(self.report_manifest)

    def _discover_transcripts(self, owners: dict[str, dict[str, Any]]) -> dict[str, Any]:
        counts: defaultdict[str, int] = defaultdict(int)
        bytes_by_class: defaultdict[str, int] = defaultdict(int)
        source_identity_cache: dict[str, dict[str, Any]] = {}
        for run_dir in sorted(path for path in self.working.iterdir() if path.is_dir()):
            transcript = run_dir / TRANSCRIPT_NAME
            if not transcript.is_file():
                continue
            size = transcript.stat().st_size
            raw = run_dir / RAW_CHECKPOINT_NAME
            total_size = size + (raw.stat().st_size if raw.is_file() else 0)
            classification = SAFE_TRANSCRIPT
            blocker = None
            evidence: dict[str, Any] = {"run_dir": str(run_dir), "raw_path": str(raw) if raw.is_file() else None}
            owner = owners.get(_path_key(run_dir))
            try:
                payload = _read_json(transcript)
                metadata = payload.get("metadata") if isinstance(payload, dict) else None
                if not owner:
                    raise _Keep("LEGACY_UNVERIFIED_KEEP", "run_not_owned_by_queue_state")
                if not isinstance(metadata, dict):
                    raise _Keep("LEGACY_UNVERIFIED_KEEP", "missing_transcript_metadata")
                source_value = str(metadata.get("source_video_path") or owner.get("source_path") or "")
                if not source_value:
                    raise _Keep("LEGACY_UNVERIFIED_KEEP", "missing_source_identity")
                source = Path(source_value).resolve(strict=False)
                if _path_key(source) != _path_key(owner["source_path"]):
                    raise _Keep("LEGACY_UNVERIFIED_KEEP", "queue_and_transcript_source_disagree")
                fingerprint_path = run_dir / "transcript.fingerprint.json"
                fingerprint = _read_json(fingerprint_path).get("fingerprint") if fingerprint_path.is_file() else None
                if not isinstance(fingerprint, dict) or fingerprint.get("stage") != "transcribe":
                    raise _Keep("LEGACY_UNVERIFIED_KEEP", "missing_historical_stage_fingerprint")
                video_identity = fingerprint.get("video")
                if not isinstance(video_identity, dict) or _path_key(video_identity.get("path")) != _path_key(source):
                    raise _Keep("LEGACY_UNVERIFIED_KEEP", "stage_source_identity_mismatch")
                if not source.is_file():
                    raise _Keep("LEGACY_UNVERIFIED_KEEP", "source_vod_missing")
                source_stat = source.stat()
                if int(video_identity.get("size") or -1) != source_stat.st_size:
                    raise _Keep("LEGACY_UNVERIFIED_KEEP", "source_size_changed")
                if int(video_identity.get("mtime_ns") or -1) != source_stat.st_mtime_ns:
                    raise _Keep("LEGACY_UNVERIFIED_KEEP", "source_mtime_changed")
                required = ("schema_version", "transcriber", "whisper_model_size", "whisper_language")
                if any(metadata.get(key) in (None, "") for key in required):
                    raise _Keep("LEGACY_UNVERIFIED_KEEP", "incomplete_transcription_metadata")
                source_key = _path_key(source)
                if source_key not in source_identity_cache:
                    source_identity_cache[source_key] = source_byte_identity(source)
                transcript_hash = self.journal.hash_file(transcript)
                raw_hash = None
                raw_metadata = None
                if raw.is_file():
                    raw_payload = _read_json(raw)
                    raw_metadata = raw_payload.get("metadata") if isinstance(raw_payload, dict) else None
                    if not isinstance(raw_metadata, dict):
                        raise _Keep("LEGACY_UNVERIFIED_KEEP", "raw_checkpoint_metadata_missing")
                    if _path_key(raw_metadata.get("source_video_path")) != source_key:
                        raise _Keep("LEGACY_UNVERIFIED_KEEP", "raw_checkpoint_source_mismatch")
                    if raw_metadata.get("schema_version") != metadata.get("schema_version"):
                        raise _Keep("LEGACY_UNVERIFIED_KEEP", "raw_checkpoint_schema_mismatch")
                    raw_hash = self.journal.hash_file(raw)
                descriptor = _legacy_descriptor(
                    source, source_identity_cache[source_key], fingerprint, metadata,
                    transcript_hash, raw_hash, raw_metadata,
                )
                canonical = self.transcripts.artifact_root(descriptor["artifact_id"])
                evidence.update({
                    "owner": owner,
                    "source_path": str(source),
                    "historical_stage_fingerprint": fingerprint,
                    "transcript_metadata": metadata,
                    "raw_metadata": raw_metadata,
                    "descriptor": descriptor,
                    "transcript_hash": transcript_hash,
                    "raw_hash": raw_hash,
                    "canonical_root": str(canonical),
                })
            except _Keep as keep:
                classification, blocker = keep.classification, keep.reason
                transcript_hash = None
                canonical = None
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                classification, blocker = "LEGACY_UNVERIFIED_KEEP", f"invalid_metadata:{type(exc).__name__}"
                transcript_hash = None
                canonical = None
            row = {
                "candidate_id": stable_id("phase2a", ["transcript", str(run_dir.resolve())]),
                "category": "historical_transcript",
                "original_path": str(transcript.resolve()),
                "size_bytes": total_size,
                "content_hash": transcript_hash,
                "canonical_path": str(canonical) if canonical else None,
                "classification": classification,
                "proposed_action": "REFERENCE_CANONICAL_AND_RETIRE_LOCAL" if classification == SAFE_TRANSCRIPT else "KEEP",
                "blocker": blocker,
                "evidence": evidence,
            }
            self.journal.upsert_candidate(row)
            counts[classification] += 1
            bytes_by_class[classification] += total_size
        return {"counts": dict(counts), "bytes": dict(bytes_by_class), "source_identities_hashed": len(source_identity_cache)}

    def _discover_raw_cuts(self, owners: dict[str, dict[str, Any]]) -> dict[str, Any]:
        counts: defaultdict[str, int] = defaultdict(int)
        bytes_by_class: defaultdict[str, int] = defaultdict(int)
        for raw in sorted(self.working.glob("*/raw_cuts/*")):
            if not raw.is_file():
                continue
            classification, blocker, evidence = _classify_raw(raw, owners, self.registry, self.journal)
            size = raw.stat().st_size
            row = {
                "candidate_id": stable_id("phase2a", ["raw", str(raw.resolve())]),
                "category": "historical_raw_cut",
                "original_path": str(raw.resolve()),
                "size_bytes": size,
                "content_hash": evidence.get("raw_hash"),
                "canonical_path": evidence.get("successor_path"),
                "classification": classification,
                "proposed_action": "DELETE_AFTER_REVERIFY" if classification == SAFE_RAW else "KEEP",
                "blocker": blocker,
                "evidence": evidence,
            }
            self.journal.upsert_candidate(row)
            counts[classification] += 1
            bytes_by_class[classification] += size
        return {"counts": dict(counts), "bytes": dict(bytes_by_class)}

    def _discover_broll_aliases(self) -> dict[str, Any]:
        roots = {
            "intro": self.project_root / "assets" / "broll_intro",
            "product_broll": self.project_root / "assets" / "product_broll",
        }
        groups: defaultdict[tuple[int, str], list[tuple[Path, str, str | None, int]]] = defaultdict(list)
        for role, root in roots.items():
            if not root.is_dir():
                continue
            index = 0
            for path in sorted((p for p in root.rglob("*") if p.is_file()), key=lambda p: str(p).casefold()):
                if path.name == ".gitkeep":
                    continue
                digest = self.journal.hash_file(path)
                product = path.relative_to(root).parts[0] if len(path.relative_to(root).parts) > 1 else None
                groups[(path.stat().st_size, digest)].append((path, role, product, index))
                index += 1
        safe = 0
        safe_bytes = 0
        group_count = 0
        for (_size, digest), items in groups.items():
            roles = {item[1] for item in items}
            if len(items) < 2 or roles != {"intro", "product_broll"}:
                continue
            group_count += 1
            anchor_row = sorted(items, key=lambda item: (item[1] != "product_broll", str(item[0]).casefold()))[0]
            anchor = anchor_row[0]
            logical_roles = [
                {
                    "asset_id": f"asset_{digest}", "content_sha256": digest,
                    "canonical_path": str(anchor.resolve()), "path": str(item[0].resolve()),
                    "visible_name": item[0].name, "role": item[1], "product": item[2], "ordering": item[3],
                }
                for item in items
            ]
            for path, role, product, ordering in items:
                if path == anchor:
                    continue
                blocker = _broll_registry_blocker(items, self.registry)
                evidence = {
                    "anchor_path": str(anchor.resolve()), "asset_hash": digest,
                    "role": role, "product": product, "ordering": ordering,
                    "logical_roles": logical_roles,
                }
                self.journal.upsert_candidate({
                    "candidate_id": stable_id("phase2a", ["broll", str(path.resolve())]),
                    "category": "broll_duplicate_asset",
                    "original_path": str(path.resolve()),
                    "size_bytes": path.stat().st_size,
                    "content_hash": digest,
                    "canonical_path": str(anchor.resolve()),
                    "classification": "KEEP_PINNED_OR_AMBIGUOUS" if blocker else SAFE_HARDLINK,
                    "proposed_action": "KEEP" if blocker else "REPLACE_WITH_VERIFIED_HARDLINK",
                    "blocker": blocker,
                    "evidence": evidence,
                })
                if not blocker:
                    safe += 1
                    safe_bytes += path.stat().st_size
        return {"exact_cross_role_groups": group_count, "safe_aliases": safe, "potential_deduplicated_bytes": safe_bytes}

    def _discover_exact_render_groups(self) -> dict[str, Any]:
        root = self.working / "modular_renders"
        groups: defaultdict[tuple[int, str], list[str]] = defaultdict(list)
        if root.is_dir():
            for path in root.rglob("*"):
                if path.is_file() and path.suffix.casefold() in {".mp4", ".mov", ".mkv", ".webm"}:
                    groups[(path.stat().st_size, self.journal.hash_file(path))].append(str(path.resolve()))
        duplicates = [paths for paths in groups.values() if len(paths) > 1]
        for paths in duplicates:
            for path_value in paths:
                path = Path(path_value)
                self.journal.upsert_candidate({
                    "candidate_id": stable_id("phase2a", ["modular-render", path_value]),
                    "category": "modular_render_duplicate",
                    "original_path": path_value,
                    "size_bytes": path.stat().st_size,
                    "content_hash": self.journal.hash_file(path),
                    "canonical_path": None,
                    "classification": "KEEP_DEPENDENCIES_UNVERIFIED",
                    "proposed_action": "KEEP",
                    "blocker": "logical_identity_and_regeneration_dependencies_not_fully_verified",
                    "evidence": {"exact_group": paths},
                })
        potential = sum(Path(group[0]).stat().st_size * (len(group) - 1) for group in duplicates)
        return {
            "exact_duplicate_groups": len(duplicates),
            "logical_artifacts": sum(len(group) for group in duplicates),
            "physical_copies_before": sum(len(group) for group in duplicates),
            "physical_copies_after": sum(len(group) for group in duplicates),
            "potential_bytes_retained": potential,
        }

    def _secondary_inventory(self) -> dict[str, Any]:
        roots = [
            "working/style_renders", "working/style_render_cache", "working/trends",
            "working/queue_history", "new_app/dist", "new_app/dist-desktop", ".git",
        ]
        result = {}
        for value in roots:
            path = self.project_root / value
            result[value] = {"exists": path.exists(), **asdict(measure_tree(path))} if path.exists() else {"exists": False, "bytes": 0, "files": 0}
        result["policy"] = {
            "style_and_trends": "KEEP_OWNER_OR_RUNTIME_DEPENDENCIES_UNVERIFIED",
            "queue_history": "KEEP_RECOVERY_AND_AUDIT_AUTHORITY_UNCHANGED",
            "builds": "KEEP_CURRENT_AND_PRIOR_KNOWN_RELEASE_PROVENANCE_UNRESOLVED",
            "git": "KEEP_NO_HISTORY_REWRITE",
        }
        return result

    def _discover_registry_test_metadata(self) -> dict[str, Any]:
        temp_root = Path(tempfile.gettempdir()).resolve(strict=False)
        count = 0
        declared_bytes = 0
        for artifact in self.registry.all_artifacts():
            path = Path(str(artifact["canonical_path"])).resolve(strict=False)
            try:
                path.relative_to(temp_root)
            except ValueError:
                continue
            if path.exists() or self.registry.active_references(str(artifact["artifact_id"])):
                continue
            self.journal.upsert_candidate({
                "candidate_id": stable_id("phase2a", ["test-metadata", artifact["artifact_id"]]),
                "category": "test_registry_metadata",
                "original_path": str(path),
                "size_bytes": 0,
                "content_hash": artifact.get("content_identity"),
                "canonical_path": None,
                "classification": "SAFE_RETIRE_TEST_METADATA",
                "proposed_action": "DELETE_REGISTRY_ROW_AFTER_BACKUP",
                "blocker": None,
                "evidence": {
                    "artifact": artifact,
                    "declared_size_bytes": int(artifact.get("size_bytes") or 0),
                    "path_missing": True,
                    "active_reference_count": 0,
                    "temp_root": str(temp_root),
                },
            })
            count += 1
            declared_bytes += int(artifact.get("size_bytes") or 0)
        return {"safe_rows": count, "declared_missing_bytes": declared_bytes}

    def _apply_transcripts(self) -> None:
        verified_artifacts: dict[str, Any] = {}
        for row in self.journal.candidate_rows(classification=SAFE_TRANSCRIPT):
            if row["status"] == "COMPLETED":
                continue
            evidence = row["evidence"]
            transcript = Path(row["original_path"])
            raw = Path(evidence["raw_path"]) if evidence.get("raw_path") else None
            descriptor = evidence["descriptor"]
            artifact_id = str(descriptor["artifact_id"])
            candidate_id = row["candidate_id"]
            try:
                if transcript.is_file() and self.journal.hash_file(transcript) != evidence["transcript_hash"]:
                    raise RuntimeError("transcript_changed_after_plan")
                if raw and raw.is_file() and self.journal.hash_file(raw) != evidence["raw_hash"]:
                    raise RuntimeError("raw_checkpoint_changed_after_plan")
                artifact = verified_artifacts.get(artifact_id)
                artifact_was_cached = artifact is not None
                if artifact is None:
                    artifact = self.transcripts.find(descriptor)
                if artifact is None:
                    if not transcript.is_file():
                        raise RuntimeError("original_missing_before_canonical_commit")
                    artifact = self.transcripts.import_legacy(
                        transcript, descriptor, legacy_raw_checkpoint=raw if raw and raw.is_file() else None,
                    )
                    self.journal.record_action(
                        candidate_id, "CANONICAL_COMMIT", "COMPLETED",
                        after_path=artifact.root,
                        evidence={"artifact_id": artifact.artifact_id, "manifest": artifact.manifest},
                    )
                if not artifact_was_cached:
                    if file_sha256(artifact.transcript_path) != evidence["transcript_hash"]:
                        raise RuntimeError("canonical_transcript_hash_mismatch")
                    if evidence.get("raw_hash"):
                        if not artifact.raw_checkpoint_path or file_sha256(artifact.raw_checkpoint_path) != evidence["raw_hash"]:
                            raise RuntimeError("canonical_checkpoint_hash_mismatch")
                    verified_artifacts[artifact_id] = artifact
                run_dir = transcript.parent
                reference = self.transcripts.attach(run_dir, artifact, descriptor)
                self.journal.record_action(
                    candidate_id, "REFERENCE_COMMIT", "COMPLETED",
                    before_path=transcript, after_path=reference,
                    evidence={"artifact_id": artifact.artifact_id},
                )
                retired = 0
                if transcript.is_file():
                    retired += transcript.stat().st_size
                    transcript.unlink()
                if raw and raw.is_file():
                    retired += raw.stat().st_size
                    raw.unlink()
                resolved = resolve_effective_transcript_path(run_dir)
                if resolved != artifact.transcript_path:
                    raise RuntimeError("post_retirement_transcript_resolution_failed")
                if evidence.get("raw_hash"):
                    resolved_raw = resolve_effective_raw_checkpoint_path(run_dir)
                    if resolved_raw != artifact.raw_checkpoint_path:
                        raise RuntimeError("post_retirement_checkpoint_resolution_failed")
                self.journal.record_action(
                    candidate_id, "RETIRE_REDUNDANT_LOCAL", "COMPLETED",
                    bytes_affected=retired, before_path=transcript, after_path=artifact.root,
                    evidence={"reference_path": str(reference), "resolver_verified": True},
                )
                self.journal.set_candidate_status(candidate_id, "COMPLETED")
            except Exception as exc:
                self.journal.record_action(
                    candidate_id, "TRANSCRIPT_MIGRATION", "FAILED",
                    before_path=transcript, after_path=row.get("canonical_path"), error=f"{type(exc).__name__}: {exc}",
                )
                self.journal.set_candidate_status(candidate_id, "PARTIAL", blocker=str(exc))

    def _apply_raw_cuts(self) -> None:
        owners = _queue_owner_index(self.project_root, self.working)
        for row in self.journal.candidate_rows(classification=SAFE_RAW):
            if row["status"] == "COMPLETED":
                continue
            raw = Path(row["original_path"])
            candidate_id = row["candidate_id"]
            try:
                if not raw.exists():
                    self.journal.set_candidate_status(candidate_id, "COMPLETED")
                    continue
                classification, blocker, evidence = _classify_raw(raw, owners, self.registry, self.journal)
                if classification != SAFE_RAW:
                    raise RuntimeError(f"revalidation_blocked:{classification}:{blocker}")
                if evidence.get("raw_hash") != row["content_hash"]:
                    raise RuntimeError("raw_changed_after_plan")
                size = raw.stat().st_size
                raw.unlink()
                self.journal.record_action(
                    candidate_id, "DELETE_PROVEN_TERMINAL_RAW", "COMPLETED",
                    bytes_affected=size, before_path=raw, after_path=evidence["successor_path"], evidence=evidence,
                )
                self.journal.set_candidate_status(candidate_id, "COMPLETED")
            except Exception as exc:
                self.journal.record_action(
                    candidate_id, "DELETE_PROVEN_TERMINAL_RAW", "FAILED", before_path=raw,
                    after_path=row.get("canonical_path"), error=f"{type(exc).__name__}: {exc}",
                )
                self.journal.set_candidate_status(candidate_id, "PARTIAL", blocker=str(exc))

    def _apply_broll_aliases(self) -> None:
        for row in self.journal.candidate_rows(classification=SAFE_HARDLINK):
            if row["status"] == "COMPLETED":
                continue
            alias = Path(row["original_path"])
            anchor = Path(row["canonical_path"])
            candidate_id = row["candidate_id"]
            temp = alias.with_name(f".{alias.name}.{uuid4().hex}.phase2a-link")
            try:
                if os.path.samefile(anchor, alias):
                    self.journal.set_candidate_status(candidate_id, "COMPLETED")
                    continue
                if anchor.drive.casefold() != alias.drive.casefold():
                    raise RuntimeError("hardlink_requires_same_volume")
                if self.journal.hash_file(anchor) != row["content_hash"] or self.journal.hash_file(alias) != row["content_hash"]:
                    raise RuntimeError("asset_hash_changed_after_plan")
                os.link(anchor, temp)
                if not os.path.samefile(anchor, temp) or file_sha256(temp) != row["content_hash"]:
                    raise RuntimeError("hardlink_staging_verification_failed")
                os.replace(temp, alias)
                if not os.path.samefile(anchor, alias) or file_sha256(alias) != row["content_hash"]:
                    raise RuntimeError("hardlink_post_replace_verification_failed")
                artifact_id = f"asset_{row['content_hash']}"
                self.registry.register_artifact(
                    artifact_id=artifact_id, artifact_type="BROLL_SOURCE", canonical_path=anchor,
                    size_bytes=anchor.stat().st_size, content_identity=row["content_hash"],
                    fingerprint=row["content_hash"], owner_identity="broll-role-index",
                    lifecycle_class=LifecycleClass.SOURCE, regenerable=False, pinned=True,
                    pin_reason="shared_broll_source",
                )
                for logical in row["evidence"].get("logical_roles", []):
                    self.registry.add_reference(
                        artifact_id, owner_type="asset_role", owner_id=logical["path"], role=logical["role"],
                        metadata={
                            "visible_name": logical["visible_name"], "product": logical.get("product"),
                            "ordering": logical["ordering"],
                        },
                    )
                self.journal.record_action(
                    candidate_id, "DEDUPLICATE_WITH_HARDLINK", "COMPLETED",
                    bytes_affected=row["size_bytes"], before_path=alias, after_path=anchor,
                    evidence={"samefile_verified": True, "hash": row["content_hash"]},
                )
                self.journal.set_candidate_status(candidate_id, "COMPLETED")
            except Exception as exc:
                temp.unlink(missing_ok=True)
                self.journal.record_action(
                    candidate_id, "DEDUPLICATE_WITH_HARDLINK", "FAILED",
                    before_path=alias, after_path=anchor, error=f"{type(exc).__name__}: {exc}",
                )
                self.journal.set_candidate_status(candidate_id, "PARTIAL", blocker=str(exc))

    def _apply_registry_test_metadata(self) -> None:
        temp_root = Path(tempfile.gettempdir()).resolve(strict=False)
        for row in self.journal.candidate_rows(classification="SAFE_RETIRE_TEST_METADATA"):
            if row["status"] == "COMPLETED":
                continue
            candidate_id = row["candidate_id"]
            artifact_id = str(row["evidence"]["artifact"]["artifact_id"])
            path = Path(row["original_path"]).resolve(strict=False)
            try:
                path.relative_to(temp_root)
                current = self.registry.artifact(artifact_id)
                if current is None:
                    self.journal.set_candidate_status(candidate_id, "COMPLETED")
                    continue
                if path.exists() or self.registry.active_references(artifact_id):
                    raise RuntimeError("test_metadata_no_longer_unreferenced_and_missing")
                with self.registry.transaction() as db:
                    db.execute("DELETE FROM artifacts WHERE artifact_id=?", (artifact_id,))
                    db.execute(
                        "DELETE FROM publish_operations WHERE (source_path LIKE ? OR destination_path LIKE ?)",
                        (str(temp_root) + "%", str(temp_root) + "%"),
                    )
                self.journal.record_action(
                    candidate_id, "RETIRE_TEST_REGISTRY_METADATA", "COMPLETED",
                    before_path=path,
                    evidence={"artifact_id": artifact_id, "file_deleted": False, "registry_backup_exists": True},
                )
                self.journal.set_candidate_status(candidate_id, "COMPLETED")
            except Exception as exc:
                self.journal.record_action(
                    candidate_id, "RETIRE_TEST_REGISTRY_METADATA", "FAILED",
                    before_path=path, error=f"{type(exc).__name__}: {exc}",
                )
                self.journal.set_candidate_status(candidate_id, "PARTIAL", blocker=str(exc))

    def _assert_application_idle(self) -> None:
        state_path = self.working / "video_queue_state.json"
        state = _read_json(state_path)
        if str(state.get("queue_status") or "").casefold() != "stopped":
            raise RuntimeError("Queue must be durably stopped before Phase 2A execution")


class _Keep(Exception):
    def __init__(self, classification: str, reason: str):
        super().__init__(reason)
        self.classification = classification
        self.reason = reason


def _legacy_descriptor(
    source: Path,
    source_identity: dict[str, Any],
    stage_fingerprint: dict[str, Any],
    metadata: dict[str, Any],
    transcript_hash: str,
    raw_hash: str | None,
    raw_metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    transcription = {
        "implementation": metadata.get("transcriber"),
        "historical_config_hash": stage_fingerprint.get("config_hash"),
        "model_name": stage_fingerprint.get("model_name"),
        "settings": {
            key: metadata.get(key)
            for key in (
                "whisper_model_size", "whisper_language", "whisper_beam_size", "whisper_best_of",
                "language", "timestamp_precision", "raw_word_timestamps_available",
            )
        },
    }
    alignment = {
        "implementation": metadata.get("word_alignment_backend"),
        "desired_backend": metadata.get("desired_word_alignment_backend"),
        "model": metadata.get("whisperx_align_model"),
        "language": metadata.get("whisperx_language"),
    }
    evidence = {
        "historical_stage_fingerprint": stage_fingerprint,
        "transcript_sha256": transcript_hash,
        "raw_checkpoint_sha256": raw_hash,
        "transcript_metadata_sha256": hashlib.sha256(canonical_json(metadata).encode("utf-8")).hexdigest(),
        "raw_metadata_sha256": hashlib.sha256(canonical_json(raw_metadata).encode("utf-8")).hexdigest() if raw_metadata else None,
    }
    identity = {
        "identity_kind": "legacy-content-and-provenance-v1",
        "artifact_schema_version": 1,
        "transcript_schema_version": int(metadata["schema_version"]),
        "source_byte_identity": source_identity,
        "transcription": transcription,
        "alignment": alignment,
        "historical_evidence": evidence,
    }
    fingerprint = hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()
    return {
        **identity,
        "artifact_id": f"transcript_legacy_{fingerprint}",
        "fingerprint": fingerprint,
        "source_path": str(source),
        "regenerable": False,
        "lifecycle_class": LifecycleClass.PERMANENT_STATE.value,
    }


def _queue_owner_index(project_root: Path, working: Path) -> dict[str, dict[str, Any]]:
    state_path = working / "video_queue_state.json"
    state = _read_json(state_path)
    videos = state.get("videos") or {}
    iterable = videos.values() if isinstance(videos, dict) else videos
    result: dict[str, dict[str, Any]] = {}
    for video in iterable:
        if not isinstance(video, dict):
            continue
        source = video.get("path")
        for record in [video, *(video.get("run_history") or [])]:
            if not isinstance(record, dict) or not record.get("working_dir"):
                continue
            run_path = Path(str(record["working_dir"]))
            if not run_path.is_absolute():
                run_path = project_root / run_path
            result[_path_key(run_path)] = {
                "source_path": str(Path(str(source)).resolve(strict=False)),
                "working_dir": str(run_path.resolve(strict=False)),
                "output_dir": record.get("output_dir"),
                "status": record.get("status"),
                "current_stage": record.get("current_stage"),
                "archived_at": record.get("archived_at"),
                "stages": record.get("stages") or {},
            }
    return result


def _classify_raw(
    raw: Path,
    owners: dict[str, dict[str, Any]],
    registry: ArtifactRegistry,
    journal: MigrationJournal,
) -> tuple[str, str | None, dict[str, Any]]:
    evidence: dict[str, Any] = {"run_dir": str(raw.parent.parent.resolve()), "raw_path": str(raw.resolve())}
    if raw.stat().st_size <= 0:
        return "KEEP_INCONSISTENT", "empty_raw_file", evidence
    owner = owners.get(_path_key(raw.parent.parent))
    if not owner:
        return "KEEP_UNKNOWN", "run_not_owned_by_queue_state", evidence
    evidence["owner"] = owner
    statuses = [str((stage or {}).get("status") or "").casefold() for stage in owner["stages"].values()]
    if str(owner.get("status") or "").casefold() in {"failed", "interrupted", "stopped"}:
        return "KEEP_RECOVERY", "job_terminal_state_requires_recovery", evidence
    if str(owner.get("status") or "").casefold() != "completed" or owner.get("current_stage"):
        return "KEEP_ACTIVE", "job_not_durably_completed", evidence
    if any(status in {"queued", "running", "retrying", "failed", "paused", "stopped"} for status in statuses):
        return "KEEP_RECOVERY", "stage_retry_or_recovery_state_present", evidence
    ffmpeg = owner["stages"].get("ffmpeg") or {}
    manifest_value = ffmpeg.get("manifest_path")
    if not manifest_value:
        return "KEEP_MISSING_SUCCESSOR", "manifest_path_missing", evidence
    manifest = Path(str(manifest_value)).resolve(strict=False)
    if not manifest.is_file():
        return "KEEP_MISSING_SUCCESSOR", "manifest_missing", evidence
    try:
        rows = _read_json(manifest)
    except (OSError, ValueError, json.JSONDecodeError):
        return "KEEP_INCONSISTENT", "manifest_invalid", evidence
    if not isinstance(rows, list):
        return "KEEP_INCONSISTENT", "manifest_not_a_list", evidence
    clip_id = raw.stem[:-4] if raw.stem.endswith("_raw") else raw.stem
    matches = [row for row in rows if isinstance(row, dict) and str(row.get("clip_id") or "") == clip_id]
    if len(matches) != 1:
        return "KEEP_INCONSISTENT", "manifest_clip_identity_not_unique", evidence
    row = matches[0]
    if str(row.get("status") or "").casefold() != "ok":
        return "KEEP_RECOVERY", "manifest_clip_not_successful", evidence
    if not bool(row.get("compliance_passed")) or bool(row.get("compliance_blocked")):
        return "KEEP_INCONSISTENT", "successor_validation_not_committed", evidence
    output_root = Path(str(owner.get("output_dir") or manifest.parent)).resolve(strict=False)
    possible: list[Path] = []
    for key in ("export_batch_path", "output_path"):
        if row.get(key):
            possible.append(Path(str(row[key])).resolve(strict=False))
    if row.get("output_file"):
        value = Path(str(row["output_file"]))
        possible.append(value.resolve(strict=False) if value.is_absolute() else (output_root / value).resolve(strict=False))
    successor = next((path for path in possible if path.is_file() and path.stat().st_size > 0), None)
    if successor is None:
        return "KEEP_MISSING_SUCCESSOR", "validated_successor_file_missing", evidence
    tracked = registry.artifact_for_path(raw)
    if tracked and registry.active_references(str(tracked["artifact_id"])):
        return "KEEP_ACTIVE", "artifact_registry_reference_present", evidence
    references = _local_raw_references(raw)
    if references:
        evidence["local_references"] = references
        return "KEEP_ACTIVE", "run_metadata_still_references_raw", evidence
    evidence.update({
        "clip_id": clip_id,
        "manifest_path": str(manifest),
        "manifest_hash": journal.hash_file(manifest),
        "manifest_row": row,
        "successor_path": str(successor),
        "successor_size": successor.stat().st_size,
        "successor_identity": _edge_identity(successor),
        "raw_hash": journal.hash_file(raw),
    })
    return SAFE_RAW, None, evidence


def _local_raw_references(raw: Path) -> list[str]:
    needles = {raw.name, str(raw), str(raw.resolve(strict=False))}
    references = []
    for path in raw.parent.parent.rglob("*"):
        if not path.is_file() or path == raw or path.name in {TRANSCRIPT_NAME, RAW_CHECKPOINT_NAME}:
            continue
        if path.suffix.casefold() not in {".json", ".jsonl"}:
            continue
        try:
            if path.stat().st_size > 50 * 1024 * 1024:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if any(needle in text for needle in needles):
                references.append(str(path.resolve()))
        except OSError:
            continue
    return references


def _metadata_backup_sources(project_root: Path, working: Path, journal: MigrationJournal) -> Iterable[Path]:
    direct = [
        project_root / "config.py",
        project_root / "reports" / "storage-audit.md",
        project_root / "reports" / "storage-phase1-implementation.md",
        project_root / "reports" / "storage-reconciliation.md",
        working / "video_queue_state.json",
        working / "queue_control.json",
    ]
    seen: set[str] = set()
    for path in direct:
        if path.is_file() and _path_key(path) not in seen:
            seen.add(_path_key(path))
            yield path
    for root in (working / "settings_snapshots", working / "queue_history"):
        if root.is_dir():
            for path in root.rglob("*"):
                if path.is_file() and _path_key(path) not in seen:
                    seen.add(_path_key(path))
                    yield path
    for row in journal.candidate_rows(classification=SAFE_RAW):
        manifest = Path(str(row["evidence"].get("manifest_path") or ""))
        if manifest.is_file() and _path_key(manifest) not in seen:
            seen.add(_path_key(manifest))
            yield manifest


def _broll_registry_blocker(
    items: list[tuple[Path, str, str | None, int]],
    registry: ArtifactRegistry,
) -> str | None:
    for path, _role, _product, _ordering in items:
        artifact = registry.artifact_for_path(path)
        if not artifact:
            continue
        if str(artifact.get("lifecycle_class")) in {
            LifecycleClass.FINAL.value, LifecycleClass.EXPORT.value, LifecycleClass.PENDING.value,
        } or bool(artifact.get("pinned")) and str(artifact.get("lifecycle_class")) != LifecycleClass.SOURCE.value:
            return f"pinned_artifact:{artifact['artifact_id']}"
        foreign_refs = [
            ref for ref in registry.active_references(str(artifact["artifact_id"]))
            if ref.get("owner_type") != "asset_role"
        ]
        if foreign_refs:
            return f"ambiguous_non_role_references:{artifact['artifact_id']}"
    return None


def _backup_row(source: Path, target: Path, integrity: str) -> dict[str, Any]:
    return {
        "backup_id": stable_id("backup", str(source.resolve())),
        "source_path": str(source.resolve()),
        "backup_path": str(target.resolve()),
        "source_size": source.stat().st_size,
        "backup_size": target.stat().st_size,
        "sha256": file_sha256(target),
        "integrity_status": integrity,
        "created_at": utc_now(),
    }


def _journal_summary(candidates: list[dict[str, Any]], actions: list[dict[str, Any]]) -> dict[str, Any]:
    classifications: defaultdict[str, dict[str, int]] = defaultdict(lambda: {"count": 0, "bytes": 0})
    statuses: defaultdict[str, int] = defaultdict(int)
    for row in candidates:
        classifications[row["classification"]]["count"] += 1
        classifications[row["classification"]]["bytes"] += int(row["size_bytes"])
        statuses[row["status"]] += 1
    completed = [row for row in actions if row["state"] == "COMPLETED"]
    return {
        "candidate_count": len(candidates),
        "classifications": dict(classifications),
        "statuses": dict(statuses),
        "completed_action_count": len(completed),
        "bytes_affected_by_completed_actions": sum(int(row["bytes_affected"]) for row in completed),
        "failure_count": sum(row["state"] == "FAILED" for row in actions),
    }


def _compact_candidate(row: dict[str, Any]) -> dict[str, Any]:
    compact = dict(row)
    evidence = dict(row.get("evidence") or {})
    if row.get("category") == "historical_transcript" and evidence.get("descriptor"):
        descriptor = evidence["descriptor"]
        owner = evidence.get("owner") or {}
        stage = evidence.get("historical_stage_fingerprint") or {}
        compact["evidence"] = {
            "run_dir": evidence.get("run_dir"),
            "raw_path": evidence.get("raw_path"),
            "source_path": evidence.get("source_path"),
            "artifact_id": descriptor.get("artifact_id"),
            "artifact_fingerprint": descriptor.get("fingerprint"),
            "identity_kind": descriptor.get("identity_kind"),
            "transcript_schema_version": descriptor.get("transcript_schema_version"),
            "source_byte_identity": descriptor.get("source_byte_identity"),
            "historical_config_hash": stage.get("config_hash"),
            "historical_model_name": stage.get("model_name"),
            "transcript_hash": evidence.get("transcript_hash"),
            "raw_hash": evidence.get("raw_hash"),
            "owner_status": owner.get("status"),
            "owner_working_dir": owner.get("working_dir"),
            "canonical_root": evidence.get("canonical_root"),
        }
    elif row.get("category") == "historical_raw_cut" and evidence.get("manifest_row"):
        compact["evidence"] = {
            key: evidence.get(key)
            for key in (
                "run_dir", "raw_path", "clip_id", "manifest_path", "manifest_hash",
                "successor_path", "successor_size", "successor_identity", "raw_hash", "local_references",
            )
        }
    return compact


def _planned_reclamation(journal: MigrationJournal) -> dict[str, Any]:
    transcripts = journal.candidate_rows(classification=SAFE_TRANSCRIPT)
    unique: dict[str, int] = {}
    for row in transcripts:
        artifact_id = str(row["evidence"]["descriptor"]["artifact_id"])
        unique.setdefault(artifact_id, int(row["size_bytes"]))
    transcript_local = sum(int(row["size_bytes"]) for row in transcripts)
    transcript_canonical = sum(unique.values())
    raw = sum(int(row["size_bytes"]) for row in journal.candidate_rows(classification=SAFE_RAW))
    broll = sum(int(row["size_bytes"]) for row in journal.candidate_rows(classification=SAFE_HARDLINK))
    return {
        "transcript_local_bytes": transcript_local,
        "transcript_unique_canonical_artifacts": len(unique),
        "transcript_canonical_payload_bytes": transcript_canonical,
        "transcript_net_bytes": transcript_local - transcript_canonical,
        "raw_bytes": raw,
        "broll_physical_dedup_bytes": broll,
        "total_project_physical_bytes": transcript_local - transcript_canonical + raw + broll,
    }


def _edge_identity(path: Path, sample: int = 1024 * 1024) -> str:
    stat = path.stat()
    digest = hashlib.sha256(stat.st_size.to_bytes(8, "big"))
    with path.open("rb") as handle:
        digest.update(handle.read(sample))
        if stat.st_size > sample:
            handle.seek(max(0, stat.st_size - sample))
            digest.update(handle.read(sample))
    return f"sha256-size-edges-v1:{digest.hexdigest()}"


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _path_key(path: str | Path | None) -> str:
    if not path:
        return ""
    return str(Path(str(path)).resolve(strict=False)).casefold()


def _write_json_atomic(path: Path, payload: Any) -> None:
    temp = path.with_suffix(path.suffix + f".{os.getpid()}.{uuid4().hex}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def _external_output_inventory(root: Path) -> dict[str, Any]:
    totals: defaultdict[str, dict[str, int]] = defaultdict(lambda: {"files": 0, "bytes": 0})
    if root.is_dir():
        for base, _dirs, files in os.walk(root):
            base_path = Path(base)
            for name in files:
                path = base_path / name
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                lifecycle = _external_output_lifecycle(path, root)
                totals[lifecycle]["files"] += 1
                totals[lifecycle]["bytes"] += size
    total_bytes = sum(row["bytes"] for row in totals.values())
    total_files = sum(row["files"] for row in totals.values())
    potential = sum(totals[name]["bytes"] for name in ("REVIEW", "REJECTED", "REGENERABLE"))
    return {
        "root": str(root),
        "exists": root.is_dir(),
        "files": total_files,
        "bytes": total_bytes,
        "lifecycle": dict(sorted(totals.items())),
        "proven_safe_reclaimable_bytes": 0,
        "potential_reclaimable_bytes": potential,
        "potential_definition": "REVIEW + REJECTED + explicitly named REGENERABLE tiers; ownership remains unverified",
        "deletion_performed": False,
        "hashing_performed": False,
    }


def _external_output_lifecycle(path: Path, root: Path) -> str:
    parts = {part.casefold() for part in path.relative_to(root).parts[:-1]}
    if "export_batches" in parts or "export_ready" in parts:
        return "EXPORT"
    if "_pending" in parts or "pending" in parts:
        return "PENDING"
    if "final" in parts or "finals" in parts:
        return "FINAL"
    if "review_needed" in parts or "review" in parts:
        return "REVIEW"
    if "rejected" in parts:
        return "REJECTED"
    if "regenerable" in parts:
        return "REGENERABLE"
    return "UNKNOWN"


def _external_vod_inventory(root: Path) -> dict[str, Any]:
    total = 0
    count = 0
    oldest_ns: int | None = None
    newest_ns: int | None = None
    if root.is_dir():
        for base, _dirs, files in os.walk(root):
            for name in files:
                try:
                    stat = (Path(base) / name).stat()
                except OSError:
                    continue
                count += 1
                total += stat.st_size
                oldest_ns = stat.st_mtime_ns if oldest_ns is None else min(oldest_ns, stat.st_mtime_ns)
                newest_ns = stat.st_mtime_ns if newest_ns is None else max(newest_ns, stat.st_mtime_ns)
    return {
        "root": str(root),
        "exists": root.is_dir(),
        "files": count,
        "bytes": total,
        "lifecycle": "SOURCE",
        "proven_safe_reclaimable_bytes": 0,
        "deletion_performed": False,
        "hashing_performed": False,
        "oldest_mtime_ns": oldest_ns,
        "newest_mtime_ns": newest_ns,
    }


def _project_ownership_metrics(
    project_root: Path,
    registry: ArtifactRegistry,
    journal: MigrationJournal,
) -> dict[str, Any]:
    tracked_paths: set[str] = set()
    pinned_paths: set[str] = set()
    for artifact in registry.all_artifacts():
        key = _path_key(artifact["canonical_path"])
        tracked_paths.add(key)
        if artifact.get("pinned"):
            pinned_paths.add(key)
    for artifact in registry.all_artifacts():
        for reference in registry.active_references(str(artifact["artifact_id"])):
            if reference.get("owner_type") == "asset_role":
                tracked_paths.add(_path_key(reference.get("owner_id")))
                pinned_paths.add(_path_key(reference.get("owner_id")))
    tracked_bytes = 0
    protected_bytes = 0
    unknown_bytes = 0
    total_bytes = 0
    file_count = 0
    seen_physical: set[tuple[int, int]] = set()
    for base, _dirs, files in os.walk(project_root):
        for name in files:
            path = Path(base) / name
            try:
                stat = path.stat()
            except OSError:
                continue
            file_count += 1
            total_bytes += stat.st_size
            key = _path_key(path)
            if key in tracked_paths or "working\\artifacts\\transcripts" in key or path.name == REFERENCE_NAME:
                identity = (int(stat.st_dev), int(stat.st_ino))
                if identity not in seen_physical:
                    tracked_bytes += stat.st_size
                    seen_physical.add(identity)
                if key in pinned_paths or "working\\artifacts\\transcripts" in key:
                    protected_bytes += stat.st_size
            else:
                unknown_bytes += stat.st_size
    pending_safe = [
        row for row in journal.candidate_rows()
        if row["classification"] in {SAFE_TRANSCRIPT, SAFE_RAW, SAFE_HARDLINK} and row["status"] != "COMPLETED"
    ]
    return {
        "generated_at": utc_now(),
        "project_bytes": total_bytes,
        "project_files": file_count,
        "working": asdict(measure_tree(project_root / "working")),
        "tracked_physical_bytes": tracked_bytes,
        "unknown_or_unclassified_bytes": unknown_bytes,
        "protected_tracked_path_bytes": protected_bytes,
        "proven_safe_reclaimable_bytes": sum(int(row["size_bytes"]) for row in pending_safe),
        "proven_safe_candidate_count": len(pending_safe),
        "unknown_policy": "KEEP",
    }
