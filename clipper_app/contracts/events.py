from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class OperationKind(StrEnum):
    PIPELINE = "pipeline"
    QUEUE = "queue"
    SCORING = "scoring"
    COMPLIANCE = "compliance"
    MODULE_ASSEMBLY = "module_assembly"
    MODULE_VALIDATION = "module_validation"
    MODULE_REVIEW = "module_review"
    MODULE_REPORT = "module_report"
    HEALTH = "health"


class Stage(StrEnum):
    INIT = "init"
    TRANSCRIBE = "transcribe"
    MOMENTS = "moments"
    LLM = "llm"
    VISION = "vision"
    YOLO = "yolo"
    MODULES = "modules"
    MODULAR = "modular"
    EDITING = "editing"
    FFMPEG = "ffmpeg"
    SCORING = "scoring"
    EXPORT_BATCHES = "export_batches"
    DONE = "done"


class EventKind(StrEnum):
    PROGRESS = "progress"
    CLIP_BATCH_START = "clip_batch_start"
    CLIP_STARTED = "clip_started"
    CLIP_COMPLETE = "clip_complete"
    CLIP_SCORING_PROGRESS = "clip_scoring_progress"
    RENDER_PAUSED = "render_paused"
    MODULE_EXTRACTION_COMPLETE = "module_extraction_complete"
    MODULAR_CLIP_COMPLETE = "modular_clip_complete"
    MODULE_ASSEMBLY_COMPLETE = "module_assembly_complete"
    PIPELINE_COMPLETE = "pipeline_complete"


class OperationStatus(StrEnum):
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"
    SKIPPED = "skipped"
    BLOCKED = "blocked"


class Severity(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProgressMetrics(StrictModel):
    clips_total: int | None = Field(default=None, ge=0)
    clips_completed: int | None = Field(default=None, ge=0)
    clips_created: int | None = Field(default=None, ge=0)
    clips_failed: int | None = Field(default=None, ge=0)
    clips_skipped: int | None = Field(default=None, ge=0)
    clips_blocked: int | None = Field(default=None, ge=0)
    clips_scored: int | None = Field(default=None, ge=0)
    active_clip_renders: int | None = Field(default=None, ge=0)
    modules_accepted: int | None = Field(default=None, ge=0)
    modules_existing: int | None = Field(default=None, ge=0)
    modules_rejected: int | None = Field(default=None, ge=0)
    export_batches_packaged: int | None = Field(default=None, ge=0)


class ArtifactReferences(StrictModel):
    output_dir: str | None = None
    manifest_path: str | None = None
    render_state_path: str | None = None
    scores_summary_path: str | None = None
    export_batch_manifest: str | None = None


class ProgressEvent(StrictModel):
    operation_id: str = Field(default_factory=lambda: uuid4().hex)
    operation: OperationKind = OperationKind.PIPELINE
    occurred_at: datetime = Field(default_factory=lambda: datetime.now().astimezone())
    kind: EventKind = EventKind.PROGRESS
    stage: Stage
    percent: int = Field(ge=0, le=100)
    message: str
    severity: Severity = Severity.INFO
    video_path: str | None = None
    clip_id: str | None = None
    clip_status: str | None = None
    render_paused: bool | None = None
    metrics: ProgressMetrics = Field(default_factory=ProgressMetrics)
    artifacts: ArtifactReferences = Field(default_factory=ArtifactReferences)

    @classmethod
    def from_legacy(
        cls,
        stage: str,
        percent: int,
        message: str,
        *,
        operation_id: str,
        operation: OperationKind = OperationKind.PIPELINE,
        video_path: str | Path | None = None,
        **payload: Any,
    ) -> "ProgressEvent":
        metric_names = set(ProgressMetrics.model_fields)
        artifact_names = set(ArtifactReferences.model_fields)
        metrics = {key: payload[key] for key in metric_names if payload.get(key) is not None}
        artifacts = {key: payload[key] for key in artifact_names if payload.get(key) is not None}
        return cls(
            operation_id=operation_id,
            operation=operation,
            kind=EventKind(payload.get("event") or EventKind.PROGRESS),
            stage=Stage(stage),
            percent=max(0, min(100, int(percent))),
            message=str(message),
            video_path=str(video_path) if video_path else None,
            clip_id=str(payload["clip_id"]) if payload.get("clip_id") else None,
            clip_status=str(payload["clip_status"]) if payload.get("clip_status") else None,
            render_paused=payload.get("render_paused"),
            metrics=ProgressMetrics(**metrics),
            artifacts=ArtifactReferences(**artifacts),
        )

    def to_legacy_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if self.kind != EventKind.PROGRESS:
            payload["event"] = self.kind.value
        if self.clip_id is not None:
            payload["clip_id"] = self.clip_id
        if self.clip_status is not None:
            payload["clip_status"] = self.clip_status
        if self.render_paused is not None:
            payload["render_paused"] = self.render_paused
        payload.update(self.metrics.model_dump(exclude_none=True))
        payload.update(self.artifacts.model_dump(exclude_none=True))
        return payload
