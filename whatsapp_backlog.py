from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

import portalocker

from clipper_app.application.whatsapp_delivery import (
    WhatsAppConflict,
    WhatsAppDeliveryService,
)
from whatsapp_media import (
    Classification,
    MediaPolicy,
    ProcessingAction,
    classify_media,
    copy_media,
    full_decode,
    probe_media,
    remux_media,
    retry_bitrate,
    source_identity,
    transcode_media,
    validate_delivery,
)


VIDEO_SUFFIXES = {".mp4", ".m4v", ".mov", ".3gp"}
RUN_STATES = {"created", "running", "resumable", "completed", "abandoned"}
LEGACY_STALE_NCLX_POLICY_REVISION = "whatsapp-media-v3-clipper-stale-nclx"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class BatchSource:
    batch_number: int
    source_folder: Path


@dataclass
class InventoryReport:
    source: str
    destination: str
    validation_level: str
    total_files: int = 0
    total_source_bytes: int = 0
    total_duration_seconds: float = 0.0
    classifications: Counter[str] = field(default_factory=Counter)
    validation_statuses: Counter[str] = field(default_factory=Counter)
    resolution_distribution: Counter[str] = field(default_factory=Counter)
    duration_distribution: Counter[str] = field(default_factory=Counter)
    codec_profile_distribution: Counter[str] = field(default_factory=Counter)
    frame_rate_distribution: Counter[str] = field(default_factory=Counter)
    color_distribution: Counter[str] = field(default_factory=Counter)
    invalid_numeric_batches: list[str] = field(default_factory=list)
    destination_conflicts: list[str] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    estimated_destination_bytes: int = 0
    estimated_temporary_bytes: int = 0
    elapsed_seconds: float = 0.0
    probe_seconds: float = 0.0
    sample_decode_seconds: float = 0.0
    full_decode_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        payload = dict(self.__dict__)
        for key in (
            "classifications",
            "validation_statuses",
            "resolution_distribution",
            "duration_distribution",
            "codec_profile_distribution",
            "frame_rate_distribution",
            "color_distribution",
        ):
            payload[key] = dict(payload[key])
        payload["structural_validation_note"] = (
            "Probe-only results are structurally_compliant_decode_unverified"
            if self.validation_level == "probe"
            else None
        )
        return payload


def load_source_ledger(
    source: str | Path, ledger: str | Path | None = None
) -> tuple[list[BatchSource], list[str]]:
    source_root = Path(source).expanduser().resolve()
    invalid: list[str] = []
    batches: list[BatchSource] = []
    if ledger:
        data = json.loads(Path(ledger).read_text(encoding="utf-8"))
        rows = data.get("batches", data) if isinstance(data, dict) else data
        for row in rows:
            batch = int(row["batch_number"])
            folder = Path(row["source_folder"]).expanduser().resolve()
            batches.append(BatchSource(batch, folder))
    else:
        if not source_root.exists():
            raise FileNotFoundError(source_root)
        for item in source_root.iterdir():
            if not item.is_dir():
                continue
            if item.name.startswith("_"):
                continue
            if not item.name.isdigit():
                invalid.append(item.name)
                continue
            batches.append(BatchSource(int(item.name), item.resolve()))
    seen: set[int] = set()
    for item in batches:
        if item.batch_number in seen:
            raise ValueError(f"Duplicate batch number in source ledger: {item.batch_number}")
        seen.add(item.batch_number)
    return sorted(batches, key=lambda item: item.batch_number), sorted(invalid)


def select_batches(
    batches: list[BatchSource],
    *,
    batch: int | None = None,
    batch_range: tuple[int, int] | None = None,
) -> list[BatchSource]:
    selected = batches
    if batch is not None:
        selected = [item for item in selected if item.batch_number == batch]
    if batch_range is not None:
        start, end = batch_range
        selected = [
            item for item in selected if min(start, end) <= item.batch_number <= max(start, end)
        ]
    return selected


def _media_files(folder: Path) -> list[Path]:
    return sorted(
        (
            item
            for item in folder.rglob("*")
            if item.is_file() and item.suffix.casefold() in VIDEO_SUFFIXES
        ),
        key=lambda item: str(item.relative_to(folder)).casefold(),
    )


def _duration_bucket(duration: float | None) -> str:
    if duration is None:
        return "unknown"
    if duration < 15:
        return "<15s"
    if duration < 30:
        return "15-30s"
    if duration < 45:
        return "30-45s"
    if duration <= 60:
        return "45-60s"
    return ">60s"


def _decode_sample(path: Path, duration: float | None) -> tuple[bool, str | None]:
    if not duration or duration <= 0:
        return False, "invalid_duration"
    null_target = "NUL" if os.name == "nt" else "/dev/null"
    windows = [0.0, max(0.0, duration / 2 - 0.5), max(0.0, duration - 1.0)]
    for start in windows:
        completed = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-xerror",
                "-ss",
                f"{start:.3f}",
                "-i",
                str(path),
                "-t",
                "1",
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-f",
                "null",
                null_target,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode:
            return False, (completed.stderr or "sample decode failed").strip()
    return True, None


def inventory(
    batches: list[BatchSource],
    destination: str | Path,
    *,
    policy: MediaPolicy,
    validation_level: str = "probe",
    invalid_numeric_batches: list[str] | None = None,
) -> InventoryReport:
    if validation_level not in {"probe", "sample-decode", "full"}:
        raise ValueError("validation level must be probe, sample-decode, or full")
    destination_root = Path(destination).expanduser().resolve()
    report = InventoryReport(
        source=str(batches[0].source_folder.parent if batches else ""),
        destination=str(destination_root),
        validation_level=validation_level,
        invalid_numeric_batches=list(invalid_numeric_batches or []),
    )
    started = time.monotonic()
    for batch_source in batches:
        destination_batch = destination_root / str(batch_source.batch_number)
        if destination_batch.exists():
            report.destination_conflicts.append(str(destination_batch))
        for source in _media_files(batch_source.source_folder):
            report.total_files += 1
            try:
                report.total_source_bytes += source.stat().st_size
                probe_started = time.monotonic()
                probe = probe_media(source)
                report.probe_seconds += time.monotonic() - probe_started
                classified = classify_media(probe, policy)
                report.total_duration_seconds += float(probe.duration_seconds or 0.0)
                label = classified.classification.value
                report.classifications[label] += 1
                if validation_level == "probe":
                    report.validation_statuses[
                        "structurally_compliant_decode_unverified"
                        if classified.classification is Classification.COPY
                        else "structurally_classified_decode_unverified"
                    ] += 1
                elif validation_level == "sample-decode":
                    report.validation_statuses["sample_decode_pending"] += 1
                else:
                    report.validation_statuses["full_decode_pending"] += 1
                report.resolution_distribution[
                    f"{probe.width or 0}x{probe.height or 0}"
                ] += 1
                report.duration_distribution[_duration_bucket(probe.duration_seconds)] += 1
                report.codec_profile_distribution[
                    f"{probe.video_codec or 'unknown'}/{probe.video_profile or 'unknown'}"
                ] += 1
                report.frame_rate_distribution[
                    f"{probe.avg_frame_rate or probe.r_frame_rate or 'unknown'}:{probe.source_frame_rate_mode}"
                ] += 1
                report.color_distribution[
                    "/".join(
                        (
                            probe.pixel_format or "unknown",
                            probe.color_range or "unknown",
                            probe.color_space or "unknown",
                            probe.color_primaries or "unknown",
                            probe.color_transfer or "unknown",
                        )
                    )
                ] += 1
                if validation_level == "sample-decode" and not probe.probe_error:
                    decode_started = time.monotonic()
                    passed, error = _decode_sample(source, probe.duration_seconds)
                    report.sample_decode_seconds += time.monotonic() - decode_started
                    if not passed:
                        report.errors.append(
                            {"path": str(source), "error": error or "sample_decode_failed"}
                        )
                        report.validation_statuses["sample_decode_failed"] += 1
                    else:
                        report.validation_statuses["sample_decode_passed"] += 1
                elif validation_level == "full" and not probe.probe_error:
                    decode_started = time.monotonic()
                    passed, error = full_decode(source)
                    report.full_decode_seconds += time.monotonic() - decode_started
                    if not passed:
                        report.errors.append(
                            {"path": str(source), "error": error or "full_decode_failed"}
                        )
                        report.validation_statuses["full_decode_failed"] += 1
                    else:
                        report.validation_statuses["full_decode_passed"] += 1
                if classified.classification is Classification.COPY:
                    estimate = probe.size_bytes
                elif classified.classification is Classification.REMUX:
                    estimate = min(probe.size_bytes + 64_000, policy.max_bytes)
                elif classified.classification is Classification.TRANSCODE:
                    estimate = policy.target_bytes
                else:
                    estimate = 0
                report.estimated_destination_bytes += estimate
                report.estimated_temporary_bytes = max(
                    report.estimated_temporary_bytes, estimate
                )
            except Exception as exc:
                report.classifications["unsupported"] += 1
                report.errors.append({"path": str(source), "error": str(exc)})
    report.elapsed_seconds = time.monotonic() - started
    return report


class BacklogCoordinator:
    def __init__(
        self,
        source_root: str | Path,
        destination_root: str | Path,
        batches: list[BatchSource],
        *,
        policy: MediaPolicy,
        workers: int = 1,
        stop_on_batch_failure: bool = False,
        adopt_existing: bool = False,
        relevant_options: dict[str, Any] | None = None,
    ) -> None:
        self.source_root = Path(source_root).expanduser().resolve()
        self.destination_root = Path(destination_root).expanduser().resolve()
        self.batches = sorted(batches, key=lambda item: item.batch_number)
        self.policy = policy
        self.workers = max(1, int(workers))
        self.stop_on_batch_failure = bool(stop_on_batch_failure)
        self.adopt_existing = bool(adopt_existing)
        self.relevant_options = relevant_options or {}
        self.state = WhatsAppDeliveryService(
            self.destination_root / "_whatsapp_state.sqlite3",
            self.destination_root,
        )
        self.run_id: str | None = None

    def _identity(self) -> tuple[str, str, str]:
        selection = json.dumps(
            [item.batch_number for item in self.batches], separators=(",", ":")
        )
        options = json.dumps(self.relevant_options, sort_keys=True, separators=(",", ":"))
        return selection, options, self.policy.fingerprint()

    def _validate_paths(self) -> None:
        source = self.source_root
        destination = self.destination_root
        if source == destination or source in destination.parents or destination in source.parents:
            raise ValueError("Source and destination must not overlap")
        for batch in self.batches:
            if destination == batch.source_folder or destination in batch.source_folder.parents:
                raise ValueError("Destination must not be inside a historical source folder")

    def find_resume_run(self, explicit_run_id: str | None = None) -> str:
        self.state.ensure_schema()
        selection, options, fingerprint = self._identity()
        with self.state.transaction() as connection:
            if explicit_run_id:
                rows = connection.execute(
                    "SELECT * FROM processing_runs WHERE run_id=? AND status IN ('resumable','running')",
                    (explicit_run_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM processing_runs
                    WHERE status IN ('resumable','running')
                      AND canonical_source_path=?
                      AND canonical_destination_path=?
                      AND policy_revision=?
                      AND configuration_fingerprint=?
                      AND normalized_batch_selection=?
                      AND relevant_cli_options=?
                    ORDER BY created_at
                    """,
                    (
                        str(self.source_root),
                        str(self.destination_root),
                        self.policy.revision,
                        fingerprint,
                        selection,
                        options,
                    ),
                ).fetchall()
        if not rows:
            raise RuntimeError("No compatible resumable run was found")
        if len(rows) > 1:
            raise RuntimeError("Multiple resumable runs match; use --resume-run")
        row = rows[0]
        if (
            row["policy_revision"] != self.policy.revision
            or row["configuration_fingerprint"] != fingerprint
            or row["normalized_batch_selection"] != selection
            or row["relevant_cli_options"] != options
        ):
            raise RuntimeError("Resume run does not match policy, configuration, or batch selection")
        return str(row["run_id"])

    def migrate_run_policy(self, run_id: str) -> dict[str, Any]:
        """Migrate one stopped resumable run to the current compatible policy.

        This deliberately accepts only the immediately previous stale-nclx
        revision.  It updates run metadata in place, leaves attempts and staged
        files untouched, and records the migration in ``packaging_state`` so a
        resume can revalidate/adopt valid outputs instead of re-encoding them.
        """
        self._validate_paths()
        self.state.ensure_schema()
        selection, options, fingerprint = self._identity()
        with self.state.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM processing_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if not row:
                raise RuntimeError("Run was not found")
            if row["status"] != "resumable":
                raise RuntimeError(
                    f"Only a stopped resumable run can be migrated (status={row['status']})"
                )
            lock = connection.execute(
                "SELECT * FROM processing_run_locks WHERE destination_path=?",
                (str(self.destination_root),),
            ).fetchone()
            if lock:
                raise RuntimeError(
                    f"Destination lock is held by run {lock['run_id']}; migration refused"
                )
            if (
                row["canonical_source_path"] != str(self.source_root)
                or row["canonical_destination_path"] != str(self.destination_root)
                or row["normalized_batch_selection"] != selection
                or row["relevant_cli_options"] != options
            ):
                raise RuntimeError("Run selection, paths, or CLI options do not match")
            old_revision = str(row["policy_revision"])
            if old_revision == self.policy.revision and row["configuration_fingerprint"] == fingerprint:
                return {
                    "run_id": run_id,
                    "migrated": False,
                    "old_policy_revision": old_revision,
                    "policy_revision": self.policy.revision,
                    "completed_rows_preserved": int(
                        connection.execute(
                            "SELECT COUNT(*) FROM media_processing_files WHERE run_id=? AND status='completed'",
                            (run_id,),
                        ).fetchone()[0]
                    ),
                }
            if old_revision != LEGACY_STALE_NCLX_POLICY_REVISION:
                raise RuntimeError(
                    f"Policy migration is not approved from {old_revision!r}"
                )
            completed_rows = int(
                connection.execute(
                    "SELECT COUNT(*) FROM media_processing_files WHERE run_id=? AND status='completed'",
                    (run_id,),
                ).fetchone()[0]
            )
            failed_rows = int(
                connection.execute(
                    "SELECT COUNT(*) FROM media_processing_files WHERE run_id=? AND status='failed'",
                    (run_id,),
                ).fetchone()[0]
            )
            now = _now()
            connection.execute(
                """
                UPDATE processing_runs
                SET policy_revision=?, configuration_fingerprint=?,
                    heartbeat_at=?, last_error=?
                WHERE run_id=?
                """,
                (
                    self.policy.revision,
                    fingerprint,
                    now,
                    f"policy migrated {old_revision} -> {self.policy.revision}",
                    run_id,
                ),
            )
            migration = {
                "run_id": run_id,
                "migrated": True,
                "migrated_at": now,
                "old_policy_revision": old_revision,
                "policy_revision": self.policy.revision,
                "old_configuration_fingerprint": row["configuration_fingerprint"],
                "configuration_fingerprint": fingerprint,
                "completed_rows_preserved": completed_rows,
                "failed_rows_reclassified_on_resume": failed_rows,
                "staging_preserved": True,
            }
            connection.execute(
                """
                INSERT INTO packaging_state(key,value,updated_at) VALUES(?,?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
                """,
                (
                    f"whatsapp_policy_migration:{run_id}",
                    json.dumps(migration, sort_keys=True, separators=(",", ":")),
                    now,
                ),
            )
        return migration

    def create_run(self) -> str:
        self._validate_paths()
        self.state.ensure_schema()
        selection, options, fingerprint = self._identity()
        with self.state.transaction(immediate=True) as connection:
            unfinished = connection.execute(
                """
                SELECT run_id FROM processing_runs
                WHERE canonical_destination_path=?
                  AND status IN ('created','running','resumable')
                """,
                (str(self.destination_root),),
            ).fetchall()
            if unfinished:
                ids = ", ".join(str(row["run_id"]) for row in unfinished)
                raise RuntimeError(
                    f"Unfinished destination runs exist ({ids}); resume or abandon them"
                )
            run_id = uuid4().hex
            connection.execute(
                """
                INSERT INTO processing_runs(
                    run_id,canonical_source_path,canonical_destination_path,
                    policy_revision,configuration_fingerprint,
                    normalized_batch_selection,relevant_cli_options,created_at,status
                ) VALUES(?,?,?,?,?,?,?,?,'created')
                """,
                (
                    run_id,
                    str(self.source_root),
                    str(self.destination_root),
                    self.policy.revision,
                    fingerprint,
                    selection,
                    options,
                    _now(),
                ),
            )
        return run_id

    def abandon_run(self, run_id: str) -> None:
        self.state.ensure_schema()
        with self.state.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT status FROM processing_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if not row:
                raise RuntimeError("Run was not found")
            if row["status"] == "completed":
                raise RuntimeError("Completed runs cannot be abandoned or reopened")
            if row["status"] == "running":
                raise RuntimeError("A running run must be stopped before abandonment")
            connection.execute(
                "UPDATE processing_runs SET status='abandoned',heartbeat_at=? WHERE run_id=?",
                (_now(), run_id),
            )
            connection.execute(
                "DELETE FROM processing_run_locks WHERE run_id=?", (run_id,)
            )

    def execute(self, *, resume_run: str | None = None) -> dict[str, Any]:
        self._validate_paths()
        execution_started = time.monotonic()
        self.destination_root.mkdir(parents=True, exist_ok=True)
        self.run_id = resume_run or self.create_run()
        lock_path = self.destination_root / "_backlog_coordinator.lock"
        counts: Counter[str] = Counter()
        failures: list[dict[str, Any]] = []
        with portalocker.Lock(str(lock_path), mode="a", timeout=0.1):
            self._take_run_ownership()
            try:
                self._clean_abandoned_attempts()
                self._clean_abandoned_run_temps()
                for batch in self.batches:
                    result = self._process_batch(batch)
                    counts.update(result["counts"])
                    failures.extend(result["failures"])
                    if result["failures"] and self.stop_on_batch_failure:
                        break
                selected_published = all(
                    (self.destination_root / str(batch.batch_number)).is_dir()
                    for batch in self.batches
                )
                status = "completed" if selected_published and not failures else "resumable"
                self._finish_run(status, None)
                if status == "completed":
                    run_tmp = self.destination_root / "_tmp" / self.run_id
                    if run_tmp.exists():
                        try:
                            run_tmp.rmdir()
                        except OSError:
                            pass
            except BaseException as exc:
                self._finish_run("resumable", str(exc))
                raise
        elapsed = max(0.001, time.monotonic() - execution_started)
        processed_files = sum(
            counts.get(key, 0)
            for key in (
                ProcessingAction.COPIED.value,
                ProcessingAction.REMUXED.value,
                ProcessingAction.BACKLOG_TRANSCODED.value,
            )
        )
        metrics = self._run_metrics()
        return {
            "run_id": self.run_id,
            "status": "completed" if not failures else "completed_with_failures",
            "counts": dict(counts),
            "failures": failures,
            "elapsed_seconds": round(elapsed, 3),
            "processed_clips_per_hour": round(processed_files * 3600 / elapsed, 2),
            "metrics": metrics,
        }

    def _run_metrics(self) -> dict[str, Any]:
        assert self.run_id
        with self.state.transaction() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) completed,
                       AVG(final_size_bytes) average_final_size_bytes,
                       AVG(encoding_attempts) average_attempts,
                       SUM(CASE WHEN encoding_attempts>1 THEN 1 ELSE 0 END) retried
                FROM media_processing_files
                WHERE run_id=? AND status='completed'
                """,
                (self.run_id,),
            ).fetchone()
            resolutions = {
                f"{item['final_width']}x{item['final_height']}": int(item["count"])
                for item in connection.execute(
                    """
                    SELECT final_width,final_height,COUNT(*) count
                    FROM media_processing_files
                    WHERE run_id=? AND status='completed'
                    GROUP BY final_width,final_height
                    """,
                    (self.run_id,),
                )
                if item["final_width"] and item["final_height"]
            }
        completed = int(row["completed"] or 0)
        return {
            "average_final_size_bytes": round(
                float(row["average_final_size_bytes"] or 0), 1
            ),
            "average_encoding_attempts": round(
                float(row["average_attempts"] or 0), 3
            ),
            "retry_rate": round(int(row["retried"] or 0) / max(1, completed), 4),
            "resolution_distribution": resolutions,
        }

    def _take_run_ownership(self) -> None:
        assert self.run_id
        host = socket.gethostname()
        now = _now()
        with self.state.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM processing_runs WHERE run_id=?", (self.run_id,)
            ).fetchone()
            if not row or row["status"] not in {"created", "resumable", "running"}:
                raise RuntimeError("Run is not available for ownership")
            previous_lock = connection.execute(
                "SELECT * FROM processing_run_locks WHERE destination_path=?",
                (str(self.destination_root),),
            ).fetchone()
            if row["status"] == "running":
                connection.execute(
                    "UPDATE processing_runs SET last_error=? WHERE run_id=?",
                    (f"stale ownership recovered at {now}", self.run_id),
                )
            if previous_lock and previous_lock["run_id"] != self.run_id:
                connection.execute(
                    """
                    UPDATE processing_runs SET status='resumable',
                        last_error=?,heartbeat_at=?
                    WHERE run_id=? AND status='running'
                    """,
                    (
                        f"stale lock recovered by run {self.run_id}",
                        now,
                        previous_lock["run_id"],
                    ),
                )
            connection.execute(
                """
                INSERT INTO processing_run_locks(
                    destination_path,run_id,owner_pid,owner_host,heartbeat_at
                ) VALUES(?,?,?,?,?)
                ON CONFLICT(destination_path) DO UPDATE SET
                    run_id=excluded.run_id,owner_pid=excluded.owner_pid,
                    owner_host=excluded.owner_host,heartbeat_at=excluded.heartbeat_at
                """,
                (str(self.destination_root), self.run_id, os.getpid(), host, now),
            )
            connection.execute(
                """
                UPDATE processing_runs SET status='running',heartbeat_at=?,
                    owner_pid=?,owner_host=? WHERE run_id=?
                """,
                (now, os.getpid(), host, self.run_id),
            )

    def _heartbeat(self) -> None:
        assert self.run_id
        now = _now()
        with self.state.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE processing_runs SET heartbeat_at=? WHERE run_id=?",
                (now, self.run_id),
            )
            connection.execute(
                "UPDATE processing_run_locks SET heartbeat_at=? WHERE run_id=?",
                (now, self.run_id),
            )

    def _finish_run(self, status: str, error: str | None) -> None:
        assert self.run_id
        if status not in {"completed", "resumable"}:
            raise ValueError(status)
        with self.state.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE processing_runs SET status=?,heartbeat_at=?,
                    completed_at=?,last_error=? WHERE run_id=?
                """,
                (
                    status,
                    _now(),
                    _now() if status == "completed" else None,
                    error,
                    self.run_id,
                ),
            )
            connection.execute(
                "DELETE FROM processing_run_locks WHERE run_id=?", (self.run_id,)
            )

    def _clean_abandoned_attempts(self) -> None:
        assert self.run_id
        run_tmp = self.destination_root / "_tmp" / self.run_id
        if not run_tmp.exists():
            return
        # Only remove UUID-suffixed attempt files. Valid final-name staged files
        # are preserved and revalidated by _process_file on resume.
        for item in run_tmp.rglob("*.attempt-*.mp4"):
            item.unlink(missing_ok=True)

    def _clean_abandoned_run_temps(self) -> None:
        tmp_root = (self.destination_root / "_tmp").resolve()
        if not tmp_root.exists():
            return
        with self.state.transaction() as connection:
            abandoned = [
                str(row["run_id"])
                for row in connection.execute(
                    "SELECT run_id FROM processing_runs WHERE status='abandoned'"
                )
            ]
        for run_id in abandoned:
            candidate = (tmp_root / run_id).resolve()
            if candidate.parent != tmp_root or not candidate.is_dir():
                continue
            shutil.rmtree(candidate)

    def _process_batch(self, batch: BatchSource) -> dict[str, Any]:
        assert self.run_id
        counts: Counter[str] = Counter()
        failures: list[dict[str, Any]] = []
        sources = _media_files(batch.source_folder)
        staging = self.destination_root / "_tmp" / self.run_id / str(batch.batch_number)
        final = self.destination_root / str(batch.batch_number)
        if final.exists():
            try:
                try:
                    registered = self.state.batch(batch.batch_number)
                except Exception:
                    registered = None
                if not registered and not self.adopt_existing:
                    raise WhatsAppConflict(
                        "Unexpected destination collision; use --adopt-existing after review"
                    )
                self._adopt_or_verify_existing(batch, final, sources)
                counts["complete_batches"] += 1
                return {"counts": counts, "failures": failures}
            except Exception as exc:
                self.state.mark_media_state(
                    batch.batch_number,
                    final,
                    media_state="conflict",
                    expected_file_count=len(sources),
                    reason=str(exc),
                )
                counts["conflicted_batches"] += 1
                failures.append({"batch": batch.batch_number, "error": str(exc)})
                return {"counts": counts, "failures": failures}
        staging.mkdir(parents=True, exist_ok=True)
        def process_one(source: Path) -> tuple[str, str]:
            self._heartbeat()
            relative = source.relative_to(batch.source_folder)
            destination = staging / relative
            return str(relative), self._process_file(
                batch.batch_number, source, relative, destination
            )

        with ThreadPoolExecutor(
            max_workers=self.workers, thread_name_prefix="whatsapp-backlog"
        ) as pool:
            future_map = {pool.submit(process_one, source): source for source in sources}
            for future in as_completed(future_map):
                source = future_map[future]
                relative = source.relative_to(batch.source_folder)
                try:
                    _relative_text, action = future.result()
                    counts[action] += 1
                except Exception as exc:
                    counts["failed"] += 1
                    failures.append(
                        {
                            "batch": batch.batch_number,
                            "relative_path": str(relative),
                            "error": str(exc),
                        }
                    )
        expected = {str(path.relative_to(batch.source_folder)) for path in sources}
        actual = {
            str(path.relative_to(staging))
            for path in _media_files(staging)
        }
        if failures or expected != actual:
            self.state.mark_media_state(
                batch.batch_number,
                final,
                media_state="incomplete",
                expected_file_count=len(sources),
                reason=json.dumps(failures, sort_keys=True),
            )
            counts["incomplete_batches"] += 1
            return {"counts": counts, "failures": failures}
        for source in sources:
            relative = source.relative_to(batch.source_folder)
            row = self._processing_row(batch.batch_number, relative)
            current = source_identity(source)
            if (
                not row
                or int(row["source_size_bytes"]) != current["size_bytes"]
                or int(row["source_mtime_ns"]) != current["mtime_ns"]
                or str(row["source_fast_fingerprint"]) != current["fast_fingerprint"]
            ):
                self.state.mark_media_state(
                    batch.batch_number,
                    final,
                    media_state="conflict",
                    expected_file_count=len(sources),
                    reason=f"source changed before publication: {relative}",
                )
                counts["conflicted_batches"] += 1
                failures.append(
                    {
                        "batch": batch.batch_number,
                        "relative_path": str(relative),
                        "error": "source_changed_before_publication",
                    }
                )
                return {"counts": counts, "failures": failures}
        staging.replace(final)
        self._register_published_batch(batch.batch_number, final)
        counts["complete_batches"] += 1
        counts["ready_batches"] += 1
        return {"counts": counts, "failures": failures}

    def _process_file(
        self,
        batch_number: int,
        source: Path,
        relative: Path,
        destination: Path,
    ) -> str:
        assert self.run_id
        identity = source_identity(source)
        existing = self._processing_row(batch_number, relative)
        if destination.exists() and existing:
            same_source = (
                int(existing["source_size_bytes"]) == identity["size_bytes"]
                and int(existing["source_mtime_ns"]) == identity["mtime_ns"]
                and str(existing["source_fast_fingerprint"]) == identity["fast_fingerprint"]
            )
            if same_source:
                compliance = validate_delivery(
                    destination,
                    policy=self.policy,
                    action=existing["processing_action"] or ProcessingAction.COPIED.value,
                    require_target_size=(
                        existing["processing_action"]
                        not in {ProcessingAction.COPIED.value, ProcessingAction.REMUXED.value}
                    ),
                    decode=True,
                )
                if compliance.compliant:
                    return str(existing["processing_action"])
            destination.unlink(missing_ok=True)
        probe = probe_media(source)
        classification = classify_media(probe, self.policy)
        if classification.classification is Classification.UNSUPPORTED:
            self._record_file(
                batch_number,
                relative,
                identity,
                probe.to_dict(),
                "failed",
                classification.classification.value,
                None,
                ",".join(classification.reasons),
            )
            raise RuntimeError(",".join(classification.reasons))
        destination.parent.mkdir(parents=True, exist_ok=True)
        action = {
            Classification.COPY: ProcessingAction.COPIED,
            Classification.REMUX: ProcessingAction.REMUXED,
            Classification.TRANSCODE: ProcessingAction.BACKLOG_TRANSCODED,
        }[classification.classification]
        self._record_file(
            batch_number,
            relative,
            identity,
            probe.to_dict(),
            "processing",
            classification.classification.value,
            action.value,
            None,
        )
        attempts = 0
        target_bps: int | None = None
        last_error = ""
        while attempts < self.policy.max_encode_attempts:
            attempts += 1
            temporary = destination.with_name(
                f"{destination.stem}.{uuid4().hex}.attempt-{attempts}.mp4"
            )
            attempt_id = self._start_attempt(
                batch_number, relative, attempts, action.value, target_bps
            )
            try:
                if action is ProcessingAction.COPIED:
                    copy_media(source, temporary)
                elif action is ProcessingAction.REMUXED:
                    completed = remux_media(source, temporary)
                    if completed.returncode:
                        raise RuntimeError(completed.stderr or "remux failed")
                else:
                    completed, plan = transcode_media(
                        source,
                        temporary,
                        policy=self.policy,
                        target_video_bps=target_bps,
                    )
                    target_bps = plan.target_video_bps
                    if completed.returncode:
                        raise RuntimeError(completed.stderr or "transcode failed")
                compliance = validate_delivery(
                    temporary,
                    policy=self.policy,
                    action=action,
                    expected_duration=probe.duration_seconds,
                    require_target_size=action
                    is ProcessingAction.BACKLOG_TRANSCODED,
                    decode=True,
                )
                if probe.color_policy_override:
                    compliance.diagnostics["source_color_override"] = (
                        probe.color_policy_override
                    )
                if compliance.compliant:
                    if source_identity(source) != identity:
                        raise RuntimeError("source_changed_during_processing")
                    os.replace(temporary, destination)
                    self._complete_attempt(attempt_id, True, None, compliance.to_dict())
                    self._complete_file(
                        batch_number,
                        relative,
                        action.value,
                        attempts,
                        compliance,
                    )
                    return action.value
                last_error = ",".join(compliance.failure_codes)
                self._complete_attempt(
                    attempt_id, False, last_error, compliance.to_dict()
                )
                if action is ProcessingAction.REMUXED:
                    action = ProcessingAction.BACKLOG_TRANSCODED
                    target_bps = None
                    continue
                if (
                    action is ProcessingAction.BACKLOG_TRANSCODED
                    and temporary.exists()
                    and temporary.stat().st_size > self.policy.target_bytes
                    and target_bps
                ):
                    target_bps = retry_bitrate(
                        target_bps, temporary.stat().st_size, self.policy
                    )
                    continue
                break
            except Exception as exc:
                last_error = str(exc)
                self._complete_attempt(attempt_id, False, last_error, None)
                if action is ProcessingAction.REMUXED:
                    action = ProcessingAction.BACKLOG_TRANSCODED
                    continue
                break
            finally:
                temporary.unlink(missing_ok=True)
        self._fail_file(batch_number, relative, attempts, last_error)
        raise RuntimeError(last_error or "processing_failed")

    def _register_published_batch(self, batch_number: int, final: Path) -> None:
        files: list[dict[str, Any]] = []
        for path in _media_files(final):
            compliance = validate_delivery(
                path,
                policy=self.policy,
                action=ProcessingAction.COPIED,
                decode=True,
            )
            if not compliance.compliant:
                raise WhatsAppConflict(
                    f"Published audit failed for {path.name}: {compliance.failure_codes}"
                )
            identity = source_identity(path)
            files.append(
                {
                    "relative_path": str(path.relative_to(final)),
                    "size_bytes": identity["size_bytes"],
                    "fingerprint": identity["fast_fingerprint"],
                    "compliance": compliance.to_dict(),
                }
            )
        self.state.register_media_batch(
            batch_number,
            final,
            files,
            media_state="complete",
            ready_for_delivery=True,
        )

    def _adopt_or_verify_existing(
        self, batch: BatchSource, final: Path, sources: list[Path]
    ) -> None:
        expected = {str(path.relative_to(batch.source_folder)) for path in sources}
        actual = {str(path.relative_to(final)) for path in _media_files(final)}
        if expected != actual:
            raise WhatsAppConflict("Existing destination filename set conflicts")
        self._register_published_batch(batch.batch_number, final)

    def _processing_row(
        self, batch_number: int, relative: Path
    ) -> sqlite3.Row | None:
        assert self.run_id
        with self.state.transaction() as connection:
            return connection.execute(
                """
                SELECT * FROM media_processing_files
                WHERE run_id=? AND batch_number=? AND relative_path=?
                """,
                (self.run_id, batch_number, str(relative)),
            ).fetchone()

    def _record_file(
        self,
        batch_number: int,
        relative: Path,
        identity: dict[str, Any],
        probe: dict[str, Any],
        status: str,
        classification: str,
        action: str | None,
        error: str | None,
    ) -> None:
        assert self.run_id
        with self.state.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO media_processing_files(
                    run_id,batch_number,relative_path,source_size_bytes,
                    source_mtime_ns,source_fast_fingerprint,status,classification,
                    processing_action,probe_json,duration_seconds,original_size_bytes,
                    original_width,original_height,codec,profile,has_b_frames,
                    last_error,started_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(run_id,batch_number,relative_path) DO UPDATE SET
                    source_size_bytes=excluded.source_size_bytes,
                    source_mtime_ns=excluded.source_mtime_ns,
                    source_fast_fingerprint=excluded.source_fast_fingerprint,
                    status=excluded.status,classification=excluded.classification,
                    processing_action=excluded.processing_action,
                    probe_json=excluded.probe_json,last_error=excluded.last_error,
                    started_at=excluded.started_at
                """,
                (
                    self.run_id,
                    batch_number,
                    str(relative),
                    identity["size_bytes"],
                    identity["mtime_ns"],
                    identity["fast_fingerprint"],
                    status,
                    classification,
                    action,
                    json.dumps(probe, separators=(",", ":")),
                    probe.get("duration_seconds"),
                    probe.get("size_bytes"),
                    probe.get("width"),
                    probe.get("height"),
                    probe.get("video_codec"),
                    probe.get("video_profile"),
                    probe.get("has_b_frames"),
                    error,
                    _now(),
                ),
            )

    def _start_attempt(
        self,
        batch_number: int,
        relative: Path,
        attempt: int,
        action: str,
        target_bps: int | None,
    ) -> str:
        assert self.run_id
        attempt_id = uuid4().hex
        with self.state.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO processing_attempts(
                    attempt_id,run_id,batch_number,relative_path,attempt_number,
                    processing_action,target_video_bps,started_at,status
                ) VALUES(?,?,?,?,?,?,?,?,'running')
                """,
                (
                    attempt_id,
                    self.run_id,
                    batch_number,
                    str(relative),
                    attempt,
                    action,
                    target_bps,
                    _now(),
                ),
            )
        return attempt_id

    def _complete_attempt(
        self,
        attempt_id: str,
        ok: bool,
        error: str | None,
        diagnostics: dict[str, Any] | None,
    ) -> None:
        with self.state.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE processing_attempts SET completed_at=?,status=?,error=?,
                    diagnostics_json=? WHERE attempt_id=?
                """,
                (
                    _now(),
                    "completed" if ok else "failed",
                    error,
                    json.dumps(diagnostics or {}, separators=(",", ":")),
                    attempt_id,
                ),
            )

    def _complete_file(
        self,
        batch_number: int,
        relative: Path,
        action: str,
        attempts: int,
        compliance: Any,
    ) -> None:
        assert self.run_id
        diagnostics = compliance.diagnostics
        with self.state.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE media_processing_files SET status='completed',
                    processing_action=?,final_size_bytes=?,final_width=?,
                    final_height=?,codec=?,profile=?,has_b_frames=?,
                    encoding_attempts=?,last_error=NULL,completed_at=?
                WHERE run_id=? AND batch_number=? AND relative_path=?
                """,
                (
                    action,
                    diagnostics.get("size_bytes"),
                    diagnostics.get("width"),
                    diagnostics.get("height"),
                    diagnostics.get("video_codec"),
                    diagnostics.get("video_profile"),
                    diagnostics.get("has_b_frames"),
                    attempts,
                    _now(),
                    self.run_id,
                    batch_number,
                    str(relative),
                ),
            )

    def _fail_file(
        self, batch_number: int, relative: Path, attempts: int, error: str
    ) -> None:
        assert self.run_id
        with self.state.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE media_processing_files SET status='failed',
                    encoding_attempts=?,last_error=?,completed_at=?
                WHERE run_id=? AND batch_number=? AND relative_path=?
                """,
                (
                    attempts,
                    error,
                    _now(),
                    self.run_id,
                    batch_number,
                    str(relative),
                ),
            )
