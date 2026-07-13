from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from clipper_app.application.logging_utils import (
    AUDIT_LOG_BACKUP_COUNT,
    AUDIT_LOG_MAX_BYTES,
    append_rotating_text,
)
from clipper_app.application.settings import (
    LegacyConfigProvider,
    SETTINGS_REGISTRY,
    normalize_setting_aliases,
    validate_setting_relationships,
)
from clipper_app.contracts.control_models import (
    ControlAuditEntry,
    ControlJob,
    ControlJobPage,
    ControlJobResultSummary,
    ControlJobStatus,
    ControlJobSummary,
    ControlOperation,
)
from clipper_app.contracts.models import SettingsSnapshot


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temp_path.replace(path)


def _non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    return None


def _control_job_summary(job: ControlJob) -> ControlJobSummary:
    result_summary: ControlJobResultSummary | None = None
    if job.operation == ControlOperation.EXPORT_BATCHES and job.result is not None:
        result: Mapping[str, Any] = job.result
        nested = result.get("payload")
        if isinstance(nested, Mapping):
            result = nested
        dry_run = result.get("dry_run")
        result_summary = ControlJobResultSummary(
            eligible_count=_non_negative_int(result.get("eligible_count")),
            packaged_count=_non_negative_int(result.get("packaged_count")),
            batch_size=_non_negative_int(result.get("batch_size")),
            dry_run=dry_run if isinstance(dry_run, bool) else None,
        )
    return ControlJobSummary(
        job_id=job.job_id,
        operation=job.operation,
        status=job.status,
        created_at=job.created_at,
        updated_at=job.updated_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        result_summary=result_summary,
        error=job.error,
        conflict_key=job.conflict_key,
        actor=job.actor,
    )


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
            current.update(self._validate_overrides(overrides))
            self._write_overrides(current)
            return self.settings_provider.snapshot()

    def delete(self, name: str, *, expected_revision: str | None = None) -> SettingsSnapshot:
        with self._lock:
            if name not in SETTINGS_REGISTRY:
                raise ValueError(f"Unsupported settings override: {name}")
            self._check_revision(expected_revision)
            current = self._read_overrides()
            current.pop(name, None)
            self._write_overrides(current)
            return self.settings_provider.snapshot()

    def reset(self, *, expected_revision: str | None = None) -> SettingsSnapshot:
        with self._lock:
            self._check_revision(expected_revision)
            self._write_overrides({})
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

    @staticmethod
    def _validate_overrides(overrides: Mapping[str, Any]) -> dict[str, Any]:
        unknown = sorted(set(overrides) - set(SETTINGS_REGISTRY))
        if unknown:
            raise ValueError(f"Unsupported settings override(s): {', '.join(unknown)}")
        validated: dict[str, Any] = {}
        for name, value in overrides.items():
            definition = SETTINGS_REGISTRY[name]
            validated[name] = LegacyConfigProvider._validate(definition, value)
        return validated


@dataclass
class ControlJobService:
    config_module: Any | None = None
    jobs_dir: str | Path | None = None
    audit_path: str | Path | None = None
    run_async: bool = True

    def __post_init__(self) -> None:
        if self.config_module is None:
            import config as config_module  # type: ignore

            self.config_module = config_module
        working_dir = Path(str(getattr(self.config_module, "WORKING_DIR", "working") or "working"))
        if not working_dir.is_absolute():
            working_dir = Path.cwd() / working_dir
        self.jobs_dir = Path(self.jobs_dir) if self.jobs_dir is not None else working_dir / "app_control_jobs"
        self.audit_path = Path(self.audit_path) if self.audit_path is not None else working_dir / "app_control_audit.jsonl"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._threads: dict[str, threading.Thread] = {}
        self.mark_stale_jobs_interrupted()

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
                raise JobConflictError(
                    f"{operation.value} conflicts with active job {conflict.job_id}",
                    job=rejected,
                    conflicting_job_id=conflict.job_id,
                )
            self._save_job(job)
            self._audit(job, "queued")

        if self.run_async:
            thread = threading.Thread(
                target=self._execute,
                args=(job.job_id, executor),
                name=f"clipper-control-{job.job_id[:8]}",
                daemon=True,
            )
            self._threads[job.job_id] = thread
            thread.start()
            return job

        self._execute(job.job_id, executor)
        return self.get(job.job_id) or job

    def get(self, job_id: str) -> ControlJob | None:
        path = self._job_path(job_id)
        if not path.exists():
            return None
        return self._load_job(path)

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
        page = jobs[offset : offset + limit]
        return ControlJobPage(jobs=tuple(page), total=len(jobs), limit=limit, offset=offset)

    def join(self, job_id: str, timeout: float | None = None) -> None:
        thread = self._threads.get(job_id)
        if thread is not None:
            thread.join(timeout=timeout)

    def mark_stale_jobs_interrupted(self) -> int:
        changed = 0
        with self._lock:
            for path in self.jobs_dir.glob("*.json"):
                job = self._load_job(path)
                if job is None or job.status not in {ControlJobStatus.QUEUED, ControlJobStatus.RUNNING}:
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
        return changed

    def _execute(self, job_id: str, executor: Callable[[], Any]) -> None:
        started = self.get(job_id)
        if started is None:
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
            completed = running.model_copy(
                update={
                    "status": ControlJobStatus.COMPLETED,
                    "result": self._result_payload(result),
                    "finished_at": finished_at,
                    "updated_at": finished_at,
                }
            )
            with self._lock:
                self._save_job(completed)
                self._audit(completed, "completed")
        except Exception as exc:
            finished_at = _now()
            failed = running.model_copy(
                update={
                    "status": ControlJobStatus.FAILED,
                    "error": str(exc),
                    "finished_at": finished_at,
                    "updated_at": finished_at,
                }
            )
            with self._lock:
                self._save_job(failed)
                self._audit(failed, "failed", {"exception_type": type(exc).__name__})

    def _active_conflict(self, conflict_key: str | None) -> ControlJob | None:
        if not conflict_key:
            return None
        for path in self.jobs_dir.glob("*.json"):
            job = self._load_job(path)
            if job is None or job.conflict_key != conflict_key:
                continue
            if job.status in {ControlJobStatus.QUEUED, ControlJobStatus.RUNNING}:
                return job
        return None

    @staticmethod
    def _result_payload(result: Any) -> dict[str, Any]:
        payload = _jsonable(result)
        if isinstance(payload, dict):
            return payload
        return {"value": payload}

    def _job_path(self, job_id: str) -> Path:
        safe = "".join(ch for ch in str(job_id) if ch.isalnum() or ch in {"-", "_"})
        return self.jobs_dir / f"{safe}.json"

    def _save_job(self, job: ControlJob) -> None:
        _atomic_write_json(self._job_path(job.job_id), job.model_dump(mode="json"))

    def _load_job(self, path: Path) -> ControlJob | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return ControlJob.model_validate(payload)
        except Exception:
            return None

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
