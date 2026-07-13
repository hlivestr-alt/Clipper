from __future__ import annotations

import hashlib
import json
import shutil
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from clipper_app.application.logging_utils import (
    AUDIT_LOG_BACKUP_COUNT,
    AUDIT_LOG_MAX_BYTES,
    append_rotating_text,
)
from clipper_app.application.job_scheduler import BoundedDaemonScheduler
from clipper_app.application.settings import (
    BROWSER_EDITABLE_SETTINGS,
    LegacyConfigProvider,
    SETTINGS_REGISTRY,
    normalize_setting_aliases,
    validate_setting_relationships,
)
from clipper_app.contracts.control_models import (
    ControlAuditEntry,
    ControlJob,
    ControlJobPage,
    ControlJobResultMetadata,
    ControlJobResultPreview,
    ControlJobResultSummary,
    ControlJobStatus,
    ControlJobSummary,
    ControlOperation,
)
from clipper_app.contracts.models import SettingsSnapshot


JOB_SCHEMA_VERSION = 2
JOB_RESULT_MAX_BYTES = 5 * 1024 * 1024
JOB_METADATA_RETENTION_DAYS = 30
JOB_METADATA_RETENTION_MAX_TERMINAL = 2_000
JOB_RESULT_RETENTION_DAYS = 7
JOB_RESULT_RETENTION_MAX_BYTES = 250 * 1024 * 1024
JOB_RESULT_PREVIEW_CHARS = 20_000
ACTIVE_JOB_STATUSES = {ControlJobStatus.QUEUED, ControlJobStatus.RUNNING}
TERMINAL_JOB_STATUSES = {
    ControlJobStatus.COMPLETED,
    ControlJobStatus.FAILED,
    ControlJobStatus.INTERRUPTED,
    ControlJobStatus.REJECTED,
}
INTERACTIVE_OPERATIONS = {
    ControlOperation.QUEUE_CONTROL,
    ControlOperation.SETTINGS_UPDATE,
    ControlOperation.SETTINGS_DELETE,
    ControlOperation.SETTINGS_RESET,
    ControlOperation.MODULE_REVIEW,
}
COMPUTE_HEAVY_OPERATIONS = {
    ControlOperation.RESCORE,
    ControlOperation.COMPLIANCE_SCAN,
    ControlOperation.MODULE_ASSEMBLY,
}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_jsonable(item) for item in value), key=lambda item: repr(item))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temp_path.replace(path)


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_bytes(payload)
    temp_path.replace(path)


def _stable_json_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    ).encode("utf-8")


def _non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    return None


def _result_summary(
    operation: ControlOperation,
    result_payload: Mapping[str, Any] | None,
) -> ControlJobResultSummary | None:
    if operation != ControlOperation.EXPORT_BATCHES or result_payload is None:
        return None
    result: Mapping[str, Any] = result_payload
    nested = result.get("payload")
    if isinstance(nested, Mapping):
        result = nested
    dry_run = result.get("dry_run")
    return ControlJobResultSummary(
        eligible_count=_non_negative_int(result.get("eligible_count")),
        actionable_count=_non_negative_int(result.get("actionable_count")),
        packaged_count=_non_negative_int(result.get("packaged_count")),
        pending_count=_non_negative_int(result.get("pending_count")),
        packaged_total=_non_negative_int(result.get("packaged_total")),
        batch_size=_non_negative_int(result.get("batch_size")),
        dry_run=dry_run if isinstance(dry_run, bool) else None,
    )


def _control_job_summary(job: ControlJob) -> ControlJobSummary:
    return ControlJobSummary(
        job_id=job.job_id,
        operation=job.operation,
        status=job.status,
        created_at=job.created_at,
        updated_at=job.updated_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        result_summary=job.result_summary or _result_summary(job.operation, job.result),
        error=job.error,
        conflict_key=job.conflict_key,
        actor=job.actor,
    )


def _sanitize_queue_result(payload: dict[str, Any]) -> dict[str, Any]:
    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, nested in tuple(value.items()):
                if key == "queue" and isinstance(nested, dict):
                    videos = nested.pop("videos", None)
                    if isinstance(videos, (list, dict)):
                        nested["video_count"] = len(videos)
                    else:
                        nested.setdefault("video_count", 0)
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(payload)
    return payload


def _bounded_result_bytes(payload: dict[str, Any], max_bytes: int) -> tuple[bytes, int, bool]:
    original = _stable_json_bytes(payload)
    original_bytes = len(original)
    if original_bytes <= max_bytes:
        return original, original_bytes, False

    decoded = original.decode("utf-8")

    def candidate(char_count: int) -> bytes:
        return _stable_json_bytes(
            {
                "_clipper_result_truncated": True,
                "original_bytes": original_bytes,
                "preview": decoded[:char_count],
            }
        )

    low = 0
    high = len(decoded)
    best = candidate(0)
    if len(best) > max_bytes:
        raise ValueError("result_max_bytes is too small for the truncation envelope")
    while low <= high:
        middle = (low + high) // 2
        encoded = candidate(middle)
        if len(encoded) <= max_bytes:
            best = encoded
            low = middle + 1
        else:
            high = middle - 1
    return best, original_bytes, True


class SettingsRevisionConflict(ValueError):
    pass


class JobConflictError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        job: ControlJob | None = None,
        conflicting_job_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.job = job
        self.conflicting_job_id = conflicting_job_id


class JobCapacityError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        job: ControlJob,
        lane: str,
        retry_after: int = 5,
    ) -> None:
        super().__init__(message)
        self.job = job
        self.lane = lane
        self.retry_after = retry_after


class JobResultNotFoundError(FileNotFoundError):
    pass


class JobResultExpiredError(JobResultNotFoundError):
    pass


class SettingsService:
    def __init__(self, settings_provider: LegacyConfigProvider | None = None) -> None:
        self.settings_provider = settings_provider or LegacyConfigProvider()
        self.path = self.settings_provider.overrides_path
        self._lock = threading.RLock()

    def effective_snapshot(self) -> SettingsSnapshot:
        return self.settings_provider.snapshot()

    def current_overrides(self) -> dict[str, Any]:
        with self._lock:
            return self._read_overrides()

    def update(
        self,
        overrides: Mapping[str, Any],
        *,
        expected_revision: str | None = None,
    ) -> SettingsSnapshot:
        with self._lock:
            self._check_revision(expected_revision)
            current = self._read_overrides()
            current.update(self._validate_overrides(overrides, editable_only=True))
            self._write_overrides(current)
            return self.settings_provider.snapshot()

    def delete(self, name: str, *, expected_revision: str | None = None) -> SettingsSnapshot:
        with self._lock:
            if name not in BROWSER_EDITABLE_SETTINGS:
                raise ValueError(f"Setting is operator-managed and cannot be changed in the browser: {name}")
            self._check_revision(expected_revision)
            current = self._read_overrides()
            current.pop(name, None)
            self._write_overrides(current)
            return self.settings_provider.snapshot()

    def reset(self, *, expected_revision: str | None = None) -> SettingsSnapshot:
        with self._lock:
            self._check_revision(expected_revision)
            current = self._read_overrides()
            locked = {name: value for name, value in current.items() if name not in BROWSER_EDITABLE_SETTINGS}
            self._write_overrides(locked)
            return self.settings_provider.snapshot()

    def _check_revision(self, expected_revision: str | None) -> None:
        current = self.settings_provider.snapshot().revision
        if expected_revision and expected_revision != current:
            raise SettingsRevisionConflict("Settings revision is stale; refresh before saving overrides.")

    def _read_overrides(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError(f"Could not read settings overrides: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("settings_overrides.json must contain a JSON object")
        overrides = payload.get("overrides", {})
        if overrides is None:
            return {}
        if not isinstance(overrides, dict):
            raise ValueError("settings_overrides.json overrides must be an object")
        return self._validate_overrides(normalize_setting_aliases(overrides))

    def _write_overrides(self, overrides: Mapping[str, Any]) -> None:
        validated = self._validate_overrides(overrides)
        effective = {
            name: getattr(self.settings_provider.config_module, name)
            for name in SETTINGS_REGISTRY
            if hasattr(self.settings_provider.config_module, name)
        }
        effective.update(validated)
        validate_setting_relationships(effective)
        normalized = sorted(validated.items())
        revision = hashlib.sha256(
            json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        payload = {
            "schema_version": 1,
            "updated_at": _now(),
            "revision": revision,
            "overrides": validated,
        }
        _atomic_write_json(self.path, payload)
        self.settings_provider.invalidate()

    @staticmethod
    def _validate_overrides(
        overrides: Mapping[str, Any],
        *,
        editable_only: bool = False,
    ) -> dict[str, Any]:
        unknown = sorted(set(overrides) - set(SETTINGS_REGISTRY))
        if unknown:
            raise ValueError(f"Unsupported settings override(s): {', '.join(unknown)}")
        if editable_only:
            locked = sorted(set(overrides) - set(BROWSER_EDITABLE_SETTINGS))
            if locked:
                raise ValueError(
                    "Operator-managed setting(s) cannot be changed in the browser: " + ", ".join(locked)
                )
        validated: dict[str, Any] = {}
        for name, value in overrides.items():
            definition = SETTINGS_REGISTRY[name]
            validated[name] = LegacyConfigProvider._validate(definition, value)
        return validated


@dataclass
class ControlJobService:
    config_module: Any | None = None
    jobs_dir: str | Path | None = None
    results_dir: str | Path | None = None
    audit_path: str | Path | None = None
    run_async: bool = True
    auto_migrate_legacy: bool = False
    result_max_bytes: int = JOB_RESULT_MAX_BYTES
    metadata_retention_days: int = JOB_METADATA_RETENTION_DAYS
    metadata_retention_max_terminal: int = JOB_METADATA_RETENTION_MAX_TERMINAL
    result_retention_days: int = JOB_RESULT_RETENTION_DAYS
    result_retention_max_bytes: int = JOB_RESULT_RETENTION_MAX_BYTES
    interactive_workers: int = 1
    interactive_pending: int = 16
    batch_workers: int = 2
    batch_pending: int = 8

    def __post_init__(self) -> None:
        if self.config_module is None:
            import config as config_module  # type: ignore

            self.config_module = config_module
        working_dir = Path(str(getattr(self.config_module, "WORKING_DIR", "working") or "working"))
        if not working_dir.is_absolute():
            working_dir = Path.cwd() / working_dir
        self.jobs_dir = Path(self.jobs_dir) if self.jobs_dir is not None else working_dir / "app_control_jobs"
        self.results_dir = (
            Path(self.results_dir)
            if self.results_dir is not None
            else working_dir / "app_control_job_results"
        )
        self.audit_path = Path(self.audit_path) if self.audit_path is not None else working_dir / "app_control_audit.jsonl"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        if self.auto_migrate_legacy:
            self.migrate_legacy_storage()
        self.mark_stale_jobs_interrupted()
        self.run_retention()
        self._scheduler = (
            BoundedDaemonScheduler(
                interactive_workers=self.interactive_workers,
                interactive_pending=self.interactive_pending,
                batch_workers=self.batch_workers,
                batch_pending=self.batch_pending,
            )
            if self.run_async
            else None
        )

    def submit(
        self,
        *,
        operation: ControlOperation,
        request: Mapping[str, Any] | BaseModel,
        executor: Callable[[], Any],
        actor: str = "operator",
        conflict_key: str | None = None,
    ) -> ControlJob:
        request_payload = _jsonable(request)
        if not isinstance(request_payload, dict):
            request_payload = {"value": request_payload}
        job = ControlJob(
            job_id=uuid4().hex,
            operation=operation,
            status=ControlJobStatus.QUEUED,
            created_at=_now(),
            updated_at=_now(),
            request=request_payload,
            conflict_key=conflict_key,
            actor=actor or "operator",
        )
        with self._lock:
            conflict = self._active_conflict(conflict_key)
            if conflict is not None:
                rejected = job.model_copy(
                    update={
                        "status": ControlJobStatus.REJECTED,
                        "error": f"Conflicts with active job {conflict.job_id}.",
                        "finished_at": _now(),
                        "updated_at": _now(),
                    }
                )
                self._save_job(rejected)
                self._audit(rejected, "rejected", {"conflicting_job_id": conflict.job_id})
                self._run_retention_locked()
                self._condition.notify_all()
                raise JobConflictError(
                    f"{operation.value} conflicts with active job {conflict.job_id}",
                    job=rejected,
                    conflicting_job_id=conflict.job_id,
                )
            self._save_job(job)
            if self.run_async:
                lane = self._lane(operation)
                assert self._scheduler is not None
                accepted = self._scheduler.submit(
                    lane,
                    lambda: self._execute(job.job_id, executor),
                    compute_heavy=operation in COMPUTE_HEAVY_OPERATIONS,
                )
                if not accepted:
                    now = _now()
                    rejected = job.model_copy(
                        update={
                            "status": ControlJobStatus.REJECTED,
                            "error": f"The {lane} job queue is at capacity.",
                            "finished_at": now,
                            "updated_at": now,
                        }
                    )
                    self._save_job(rejected)
                    self._audit(rejected, "rejected", {"reason": "capacity", "lane": lane})
                    self._run_retention_locked()
                    self._condition.notify_all()
                    raise JobCapacityError(
                        f"{operation.value} could not be queued because the {lane} lane is full",
                        job=rejected,
                        lane=lane,
                    )
            self._audit(job, "queued")

        if self.run_async:
            return job

        self._execute(job.job_id, executor)
        return self.get(job.job_id) or job

    def get(self, job_id: str, *, include_result: bool = True) -> ControlJob | None:
        path = self._job_path(job_id)
        if not path.exists():
            return None
        job = self._load_job(path)
        if job is None or not include_result:
            return job
        try:
            result = self.get_result(job_id)
        except JobResultNotFoundError:
            result = self._load_legacy_result(path)
        return job.model_copy(update={"result": result}) if result is not None else job

    def get_result(self, job_id: str) -> dict[str, Any]:
        path = self.result_file(job_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise JobResultNotFoundError(f"Stored result for {job_id} is unreadable") from exc
        if isinstance(payload, dict):
            return payload
        return {"value": payload}

    def result_file(self, job_id: str) -> Path:
        job = self.get(job_id, include_result=False)
        if job is None:
            raise JobResultNotFoundError(f"job_id {job_id} was not found")
        expires_at = _parse_timestamp(job.result_metadata.expires_at)
        if expires_at is not None and datetime.now(timezone.utc) >= expires_at:
            raise JobResultExpiredError(f"Stored result for {job_id} has expired")
        path = self._result_path(job_id)
        previously_retained = bool(
            job.result_metadata.original_bytes
            or job.result_metadata.stored_bytes
            or job.result_metadata.expires_at
        )
        if not job.result_metadata.available or not path.is_file():
            if previously_retained:
                raise JobResultExpiredError(f"Stored result for {job_id} has expired")
            raise JobResultNotFoundError(f"No stored result exists for {job_id}")
        return path

    def get_result_preview(
        self,
        job_id: str,
        *,
        max_chars: int = JOB_RESULT_PREVIEW_CHARS,
    ) -> ControlJobResultPreview:
        job = self.get(job_id, include_result=False)
        if job is None:
            raise JobResultNotFoundError(f"job_id {job_id} was not found")
        text = self.result_file(job_id).read_text(encoding="utf-8", errors="replace")
        max_chars = max(1, min(int(max_chars), JOB_RESULT_PREVIEW_CHARS))
        preview_truncated = len(text) > max_chars
        return ControlJobResultPreview(
            job_id=job_id,
            preview=text[:max_chars],
            truncated=job.result_metadata.truncated or preview_truncated,
            original_bytes=job.result_metadata.original_bytes,
            stored_bytes=job.result_metadata.stored_bytes,
        )

    def list(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        operation: str | None = None,
        status: str | None = None,
        actor: str | None = None,
    ) -> ControlJobPage:
        limit = max(1, min(int(limit or 50), 200))
        offset = max(0, int(offset or 0))
        jobs: list[ControlJobSummary] = []
        for path in self.jobs_dir.glob("*.json"):
            job = self._load_job(path)
            if job is not None:
                summary = _control_job_summary(job)
                del job
                jobs.append(summary)
        if operation:
            normalized_operation = str(operation).strip().casefold()
            jobs = [job for job in jobs if job.operation.value.casefold() == normalized_operation]
        if status:
            normalized_status = str(status).strip().casefold()
            jobs = [job for job in jobs if job.status.value.casefold() == normalized_status]
        if actor:
            normalized_actor = str(actor).strip().casefold()
            jobs = [job for job in jobs if job.actor.casefold() == normalized_actor]
        jobs.sort(key=lambda job: job.created_at, reverse=True)
        active_count = sum(job.status in ACTIVE_JOB_STATUSES for job in jobs)
        page = jobs[offset : offset + limit]
        return ControlJobPage(
            jobs=tuple(page),
            total=len(jobs),
            limit=limit,
            offset=offset,
            active_count=active_count,
        )

    def join(self, job_id: str, timeout: float | None = None) -> None:
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        with self._condition:
            while True:
                job = self.get(job_id, include_result=False)
                if job is None or job.status not in ACTIVE_JOB_STATUSES:
                    return
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return
                self._condition.wait(timeout=remaining)

    def mark_stale_jobs_interrupted(self) -> int:
        changed = 0
        with self._lock:
            for path in self.jobs_dir.glob("*.json"):
                job = self._load_job(path)
                if job is None or job.status not in ACTIVE_JOB_STATUSES:
                    continue
                now = _now()
                updated = job.model_copy(
                    update={
                        "status": ControlJobStatus.INTERRUPTED,
                        "updated_at": now,
                        "finished_at": job.finished_at or now,
                        "error": job.error or "Interrupted during API startup recovery.",
                    }
                )
                self._save_job(updated)
                self._audit(updated, "interrupted")
                changed += 1
            if changed:
                self._run_retention_locked()
                self._condition.notify_all()
        return changed

    def _execute(self, job_id: str, executor: Callable[[], Any]) -> None:
        started = self.get(job_id, include_result=False)
        if started is None or started.status != ControlJobStatus.QUEUED:
            return
        now = _now()
        running = started.model_copy(
            update={
                "status": ControlJobStatus.RUNNING,
                "started_at": started.started_at or now,
                "updated_at": now,
            }
        )
        with self._lock:
            self._save_job(running)
            self._audit(running, "running")
        try:
            result = executor()
            finished_at = _now()
            result_payload = self._result_payload(result)
            if running.operation == ControlOperation.QUEUE_CONTROL:
                result_payload = _sanitize_queue_result(result_payload)
            result_bytes, original_bytes, truncated = _bounded_result_bytes(
                result_payload,
                self.result_max_bytes,
            )
            expires_at = (
                (_parse_timestamp(finished_at) or datetime.now(timezone.utc))
                + timedelta(days=self.result_retention_days)
            ).isoformat(timespec="seconds")
            result_metadata = ControlJobResultMetadata(
                available=True,
                truncated=truncated,
                original_bytes=original_bytes,
                stored_bytes=len(result_bytes),
                expires_at=expires_at,
            )
            completed = running.model_copy(
                update={
                    "status": ControlJobStatus.COMPLETED,
                    "result": None,
                    "result_summary": _result_summary(running.operation, result_payload),
                    "result_metadata": result_metadata,
                    "finished_at": finished_at,
                    "updated_at": finished_at,
                }
            )
            with self._lock:
                _atomic_write_bytes(self._result_path(job_id), result_bytes)
                self._save_job(completed)
                self._audit(completed, "completed")
                self._run_retention_locked()
                self._condition.notify_all()
        except Exception as exc:
            finished_at = _now()
            self._result_path(job_id).unlink(missing_ok=True)
            failed = running.model_copy(
                update={
                    "status": ControlJobStatus.FAILED,
                    "result": None,
                    "result_metadata": ControlJobResultMetadata(),
                    "error": str(exc),
                    "finished_at": finished_at,
                    "updated_at": finished_at,
                }
            )
            with self._lock:
                self._save_job(failed)
                self._audit(failed, "failed", {"exception_type": type(exc).__name__})
                self._run_retention_locked()
                self._condition.notify_all()

    def _active_conflict(self, conflict_key: str | None) -> ControlJob | None:
        if not conflict_key:
            return None
        for path in self.jobs_dir.glob("*.json"):
            job = self._load_job(path)
            if job is None or job.conflict_key != conflict_key:
                continue
            if job.status in ACTIVE_JOB_STATUSES:
                return job
        return None

    @staticmethod
    def _result_payload(result: Any) -> dict[str, Any]:
        payload = _jsonable(result)
        if isinstance(payload, dict):
            return payload
        return {"value": payload}

    def _job_path(self, job_id: str) -> Path:
        safe = self._safe_job_id(job_id)
        return self.jobs_dir / f"{safe}.json"

    def _result_path(self, job_id: str) -> Path:
        return self.results_dir / f"{self._safe_job_id(job_id)}.json"

    def _save_job(self, job: ControlJob) -> None:
        metadata = job.model_copy(update={"schema_version": JOB_SCHEMA_VERSION, "result": None})
        _atomic_write_json(
            self._job_path(job.job_id),
            metadata.model_dump(mode="json", exclude={"result"}),
        )

    def _load_job(self, path: Path) -> ControlJob | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return None
            legacy_result = payload.pop("result", None)
            job = ControlJob.model_validate(payload)
            if job.result_summary is None and isinstance(legacy_result, Mapping):
                job = job.model_copy(
                    update={"result_summary": _result_summary(job.operation, legacy_result)}
                )
            return job.model_copy(update={"result": None})
        except Exception:
            return None

    @staticmethod
    def _load_legacy_result(path: Path) -> dict[str, Any] | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            result = payload.get("result") if isinstance(payload, dict) else None
        except Exception:
            return None
        return result if isinstance(result, dict) else None

    @staticmethod
    def _safe_job_id(job_id: str) -> str:
        raw = str(job_id)
        safe = "".join(ch for ch in raw if ch.isalnum() or ch in {"-", "_"})
        if not safe or safe != raw:
            raise ValueError("job_id does not contain a safe identifier")
        return safe

    @staticmethod
    def _lane(operation: ControlOperation) -> str:
        return "interactive" if operation in INTERACTIVE_OPERATIONS else "batch"

    def run_retention(self) -> dict[str, int]:
        with self._lock:
            return self._run_retention_locked()

    def _run_retention_locked(self) -> dict[str, int]:
        stats = {"metadata_deleted": 0, "results_deleted": 0, "bytes_reclaimed": 0}
        now = datetime.now(timezone.utc)
        metadata_cutoff = now - timedelta(days=max(0, self.metadata_retention_days))

        records: list[tuple[Path, ControlJob, datetime]] = []
        for path in self.jobs_dir.glob("*.json"):
            job = self._load_job(path)
            if job is None:
                continue
            timestamp = (
                _parse_timestamp(job.finished_at)
                or _parse_timestamp(job.updated_at)
                or _parse_timestamp(job.created_at)
                or datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            )
            records.append((path, job, timestamp))

        terminal = sorted(
            (record for record in records if record[1].status in TERMINAL_JOB_STATUSES),
            key=lambda record: record[2],
            reverse=True,
        )
        keep_count = max(0, self.metadata_retention_max_terminal)
        delete_ids: set[str] = set()
        for index, (path, job, timestamp) in enumerate(terminal):
            if timestamp >= metadata_cutoff and index < keep_count:
                continue
            result_path = self._result_path(job.job_id)
            if result_path.exists():
                size = result_path.stat().st_size
                result_path.unlink(missing_ok=True)
                stats["results_deleted"] += 1
                stats["bytes_reclaimed"] += size
            metadata_size = path.stat().st_size if path.exists() else 0
            path.unlink(missing_ok=True)
            stats["metadata_deleted"] += 1
            stats["bytes_reclaimed"] += metadata_size
            delete_ids.add(job.job_id)

        retained = {job.job_id: job for _, job, _ in records if job.job_id not in delete_ids}
        active_ids = {
            job_id for job_id, job in retained.items() if job.status in ACTIVE_JOB_STATUSES
        }
        result_cutoff = now - timedelta(days=max(0, self.result_retention_days))
        candidates: list[tuple[Path, ControlJob | None, datetime]] = []
        for path in self.results_dir.glob("*.json"):
            job = retained.get(path.stem)
            if path.stem in active_ids:
                continue
            timestamp = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            if job is not None:
                timestamp = (
                    _parse_timestamp(job.finished_at)
                    or _parse_timestamp(job.updated_at)
                    or timestamp
                )
            candidates.append((path, job, timestamp))

        def delete_result(path: Path, job: ControlJob | None) -> None:
            if not path.exists():
                return
            size = path.stat().st_size
            path.unlink(missing_ok=True)
            stats["results_deleted"] += 1
            stats["bytes_reclaimed"] += size
            if job is not None and job.result_metadata.available:
                updated_metadata = job.result_metadata.model_copy(update={"available": False})
                updated = job.model_copy(update={"result_metadata": updated_metadata, "result": None})
                self._save_job(updated)
                retained[job.job_id] = updated

        for path, job, timestamp in candidates:
            expires_at = _parse_timestamp(job.result_metadata.expires_at) if job is not None else None
            if timestamp < result_cutoff or (expires_at is not None and expires_at <= now):
                delete_result(path, job)

        for job_id, job in tuple(retained.items()):
            if (
                job.status in TERMINAL_JOB_STATUSES
                and job.result_metadata.available
                and not self._result_path(job_id).is_file()
            ):
                updated = job.model_copy(
                    update={
                        "result": None,
                        "result_metadata": job.result_metadata.model_copy(
                            update={"available": False}
                        ),
                    }
                )
                self._save_job(updated)
                retained[job_id] = updated

        remaining = [item for item in candidates if item[0].exists()]
        total_bytes = sum(path.stat().st_size for path in self.results_dir.glob("*.json"))
        byte_limit = max(0, self.result_retention_max_bytes)
        for path, job, _ in sorted(remaining, key=lambda item: item[2]):
            if total_bytes <= byte_limit:
                break
            size = path.stat().st_size
            delete_result(path, job)
            total_bytes -= size

        self._cleanup_migration_backups(now)
        return stats

    def migrate_legacy_storage(self) -> dict[str, Any]:
        """Atomically migrate embedded v1 results into bounded v2 result files."""
        with self._lock:
            report: dict[str, Any] = {
                "migrated": False,
                "jobs_migrated": 0,
                "results_migrated": 0,
                "expired_results": 0,
                "truncated_results": 0,
                "unreadable_files": [],
                "bytes_reclaimed": 0,
                "backup_path": None,
            }
            sources = sorted(self.jobs_dir.glob("*.json"))
            parsed: list[tuple[Path, dict[str, Any]]] = []
            has_legacy = False
            for path in sources:
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(payload, dict):
                        raise ValueError("job record is not an object")
                except Exception:
                    report["unreadable_files"].append(path.name)
                    continue
                parsed.append((path, payload))
                if payload.get("schema_version") != JOB_SCHEMA_VERSION or "result" in payload:
                    has_legacy = True
            if not has_legacy:
                return report

            suffix = uuid4().hex[:10]
            jobs_stage = self.jobs_dir.parent / f".{self.jobs_dir.name}.v2-staging-{suffix}"
            results_stage = self.results_dir.parent / f".{self.results_dir.name}.v2-staging-{suffix}"
            jobs_stage.mkdir(parents=True)
            shutil.copytree(self.results_dir, results_stage, dirs_exist_ok=True)
            now = datetime.now(timezone.utc)
            before_bytes = sum(path.stat().st_size for path in sources)
            before_bytes += sum(
                path.stat().st_size for path in self.results_dir.glob("*.json") if path.is_file()
            )

            try:
                for source_path, raw_payload in parsed:
                    payload = dict(raw_payload)
                    legacy_result = payload.pop("result", None)
                    try:
                        job = ControlJob.model_validate(payload).model_copy(
                            update={"schema_version": JOB_SCHEMA_VERSION, "result": None}
                        )
                        safe_job_id = self._safe_job_id(job.job_id)
                        if safe_job_id != source_path.stem:
                            raise ValueError("job_id does not match its metadata filename")
                    except Exception:
                        report["unreadable_files"].append(source_path.name)
                        continue

                    if legacy_result is not None:
                        result_payload = self._result_payload(legacy_result)
                        if job.operation == ControlOperation.QUEUE_CONTROL:
                            result_payload = _sanitize_queue_result(result_payload)
                        result_bytes, original_bytes, truncated = _bounded_result_bytes(
                            result_payload,
                            self.result_max_bytes,
                        )
                        finished_at = (
                            _parse_timestamp(job.finished_at)
                            or _parse_timestamp(job.updated_at)
                            or now
                        )
                        expires_at = finished_at + timedelta(days=self.result_retention_days)
                        available = expires_at > now
                        if available:
                            _atomic_write_bytes(results_stage / f"{safe_job_id}.json", result_bytes)
                            report["results_migrated"] += 1
                        else:
                            (results_stage / f"{safe_job_id}.json").unlink(missing_ok=True)
                            report["expired_results"] += 1
                        if truncated:
                            report["truncated_results"] += 1
                        job = job.model_copy(
                            update={
                                "result_summary": _result_summary(job.operation, result_payload),
                                "result_metadata": ControlJobResultMetadata(
                                    available=available,
                                    truncated=truncated,
                                    original_bytes=original_bytes,
                                    stored_bytes=len(result_bytes),
                                    expires_at=expires_at.isoformat(timespec="seconds"),
                                ),
                            }
                        )

                    metadata_payload = job.model_dump(mode="json", exclude={"result"})
                    _atomic_write_json(jobs_stage / source_path.name, metadata_payload)
                    report["jobs_migrated"] += 1

                for staged_path in jobs_stage.glob("*.json"):
                    payload = json.loads(staged_path.read_text(encoding="utf-8"))
                    ControlJob.model_validate(payload)

                stamp = now.strftime("%Y%m%dT%H%M%SZ")
                backup_jobs = self.jobs_dir.parent / f"{self.jobs_dir.name}.v1.backup-{stamp}-{suffix}"
                backup_results = (
                    self.results_dir.parent
                    / f"{self.results_dir.name}.pre-v2.backup-{stamp}-{suffix}"
                )
                self.jobs_dir.replace(backup_jobs)
                try:
                    jobs_stage.replace(self.jobs_dir)
                    self.results_dir.replace(backup_results)
                    results_stage.replace(self.results_dir)
                except Exception:
                    if self.jobs_dir.exists():
                        shutil.rmtree(self.jobs_dir)
                    if backup_jobs.exists():
                        backup_jobs.replace(self.jobs_dir)
                    if not self.results_dir.exists() and backup_results.exists():
                        backup_results.replace(self.results_dir)
                    raise

                try:
                    (backup_jobs / ".retained_at").write_text(_now(), encoding="utf-8")
                    if backup_results.exists():
                        (backup_results / ".retained_at").write_text(_now(), encoding="utf-8")
                except OSError:
                    pass
                after_bytes = sum(
                    path.stat().st_size for path in self.jobs_dir.glob("*.json") if path.is_file()
                ) + sum(
                    path.stat().st_size for path in self.results_dir.glob("*.json") if path.is_file()
                )
                report["migrated"] = True
                report["backup_path"] = str(backup_jobs)
                report["bytes_reclaimed"] = max(0, before_bytes - after_bytes)
                return report
            except Exception:
                if jobs_stage.exists():
                    shutil.rmtree(jobs_stage)
                if results_stage.exists():
                    shutil.rmtree(results_stage)
                raise

    def _cleanup_migration_backups(self, now: datetime) -> None:
        cutoff = now - timedelta(days=7)
        locations = (
            (self.jobs_dir.parent, f"{self.jobs_dir.name}.v1.backup-*"),
            (self.results_dir.parent, f"{self.results_dir.name}.pre-v2.backup-*"),
        )
        for parent, pattern in locations:
            for path in parent.glob(pattern):
                marker = path / ".retained_at"
                timestamp_path = marker if marker.exists() else path
                try:
                    modified = datetime.fromtimestamp(timestamp_path.stat().st_mtime, tz=timezone.utc)
                except OSError:
                    continue
                if modified < cutoff and path.is_dir():
                    shutil.rmtree(path)

    def _audit(self, job: ControlJob, message: str, detail: Mapping[str, Any] | None = None) -> None:
        entry = ControlAuditEntry(
            job_id=job.job_id,
            operation=job.operation,
            status=job.status,
            timestamp=_now(),
            message=message,
            actor=job.actor,
            detail=dict(detail or {}),
        )
        append_rotating_text(
            self.audit_path,
            json.dumps(entry.model_dump(mode="json"), ensure_ascii=False, sort_keys=True) + "\n",
            max_bytes=AUDIT_LOG_MAX_BYTES,
            backup_count=AUDIT_LOG_BACKUP_COUNT,
        )
