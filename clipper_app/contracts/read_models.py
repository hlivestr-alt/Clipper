from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictReadModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceSignature(StrictReadModel):
    path: str
    exists: bool
    mtime_ns: int = Field(default=0, ge=0)
    size: int = Field(default=0, ge=0)


class ArtifactRef(StrictReadModel):
    path: str
    url: str
    kind: Literal["video", "image", "json", "text", "unknown"] = "unknown"
    exists: bool = False


class ReadEnvelope(StrictReadModel):
    data: Any
    generated_at: datetime
    source_signatures: tuple[SourceSignature, ...] = ()
    warnings: tuple[str, ...] = ()


class QueueRunRow(StrictReadModel):
    run_id: str = ""
    video_name: str
    video_path: str | None = None
    status: str
    current_step: str
    progress: int = Field(ge=0, le=100)
    attention: str = ""
    clips_generated: int = Field(default=0, ge=0)
    runs: int = Field(default=1, ge=0)
    redos: int = Field(default=0, ge=0)
    duration: str = "-"
    started_at: str = ""
    completed_at: str = ""
    output_dir: str | None = None
    working_dir: str | None = None
    current_stage: str | None = None


class ProductionDayPoint(StrictReadModel):
    date: str
    clips: int = Field(default=0, ge=0)


class DashboardSummary(StrictReadModel):
    state_path: str
    updated_at: str | None = None
    queue_status: str = "unknown"
    queue_health: dict[str, Any] = Field(default_factory=dict)
    status_counts: dict[str, int] = Field(default_factory=dict)
    stage_running: dict[str, int] = Field(default_factory=dict)
    stage_queued: dict[str, int] = Field(default_factory=dict)
    stage_waiting: dict[str, int] = Field(default_factory=dict)
    waiting_videos: int = Field(default=0, ge=0)
    stage_admission_limit: int = Field(default=3, ge=1)
    total_videos: int = Field(default=0, ge=0)
    total_clips: int = Field(default=0, ge=0)
    clips_today: int = Field(default=0, ge=0)
    clips_last_24h: int = Field(default=0, ge=0)
    clips_per_hour: float = Field(default=0.0, ge=0)
    production_days: tuple[ProductionDayPoint, ...] = ()
    rows: tuple[QueueRunRow, ...] = ()


class QueueDetail(StrictReadModel):
    state_path: str
    updated_at: str | None = None
    queue_status: str = "unknown"
    queue_health: dict[str, Any] = Field(default_factory=dict)
    control_status: str = "unknown"
    launch_config: dict[str, Any] = Field(default_factory=dict)
    active_launch_config: dict[str, Any] = Field(default_factory=dict)
    stored_launch_config: dict[str, Any] = Field(default_factory=dict)
    launch_summary: str = ""
    stage_waiting: dict[str, int] = Field(default_factory=dict)
    waiting_videos: int = Field(default=0, ge=0)
    stage_admission_limit: int = Field(default=3, ge=1)
    rows: tuple[QueueRunRow, ...] = ()


class QueueVodFile(StrictReadModel):
    name: str
    path: str
    size: int = Field(default=0, ge=0)
    modified_at: str = ""


class QueueVodList(StrictReadModel):
    input_dir: str
    exists: bool
    files: tuple[QueueVodFile, ...] = ()


class ScoreStats(StrictReadModel):
    summary_count: int = Field(default=0, ge=0)
    previous_text_qwen_calls: int = Field(default=0, ge=0)
    actual_text_qwen_calls: int = Field(default=0, ge=0)
    saved_text_qwen_calls: int = Field(default=0, ge=0)
    actual_vision_qwen_calls: int = Field(default=0, ge=0)
    vision_base_group_count: int = Field(default=0, ge=0)
    vision_contact_sheet_groups: int = Field(default=0, ge=0)
    vision_contact_sheet_fallbacks: int = Field(default=0, ge=0)


class ScoreRow(StrictReadModel):
    score_key: str
    base_score_key: str
    row_type: Literal["base", "variant"]
    source_video: str
    run_tag: str = ""
    source_date: str = ""
    clip_id: str = ""
    product: str = "general"
    total_score: float | None = None
    content_score: float | None = None
    host_focus_score: float | None = None
    hook_score: float | None = None
    quality_score: float | None = None
    engagement_score: float | None = None
    similarity_score: float | None = None
    variants: int | None = None
    flags: tuple[str, ...] = ()
    flag_count: int = Field(default=0, ge=0)
    flag_severity: str = "none"
    status: str = "Okay"
    compliance_blocked: bool = False
    summary: str = ""
    output_file: str = ""
    clip_path: str = ""
    artifact: ArtifactRef | None = None
    scored_at: str = ""
    sort_timestamp: str = ""


class ScoreIndexPage(StrictReadModel):
    rows: tuple[ScoreRow, ...] = ()
    total: int = Field(default=0, ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)
    stats: ScoreStats = Field(default_factory=ScoreStats)
    filter_options: dict[str, tuple[str, ...]] = Field(default_factory=dict)


class ScoreDetail(StrictReadModel):
    selected: ScoreRow | None = None
    variants: tuple[ScoreRow, ...] = ()
    raw: dict[str, Any] = Field(default_factory=dict)
    base_raw: dict[str, Any] = Field(default_factory=dict)


class ComplianceRow(StrictReadModel):
    source_video: str
    run_tag: str = ""
    clip_id: str = ""
    product: str = "general"
    status: str = ""
    passed: bool = False
    blocked: bool = False
    auto_fixed: bool = False
    violation_count: int = Field(default=0, ge=0)
    summary: str = ""
    compliance_file: str = ""
    output_dir: str = ""
    checked_at: str = ""


class ComplianceViolationRow(StrictReadModel):
    source_video: str
    run_tag: str = ""
    clip_id: str = ""
    product: str = "general"
    field: str = "transcript"
    severity: str = ""
    violation_type: str = ""
    original_text: str = ""
    suggested_replacement: str = ""
    start: int | None = None
    end: int | None = None
    compliance_file: str = ""
    output_dir: str = ""
    checked_at: str = ""


class ComplianceIndexPage(StrictReadModel):
    rows: tuple[ComplianceRow, ...] = ()
    violations: tuple[ComplianceViolationRow, ...] = ()
    total: int = Field(default=0, ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)
    summary: dict[str, int] = Field(default_factory=dict)
    filter_options: dict[str, tuple[str, ...]] = Field(default_factory=dict)


class ModuleReadinessRow(StrictReadModel):
    product: str
    product_key: str
    hook: int = Field(default=0, ge=0)
    main: int = Field(default=0, ge=0)
    cta: int = Field(default=0, ge=0)
    total: int = Field(default=0, ge=0)
    readiness: Literal["ready", "partial", "empty"]
    visual_total: int = Field(default=0, ge=0)
    visual_passed: int = Field(default=0, ge=0)
    visual_failed: int = Field(default=0, ge=0)
    visual_not_run: int = Field(default=0, ge=0)
    zoom_ready_candidates: int = Field(default=0, ge=0)


class ModuleReadiness(StrictReadModel):
    library_dir: str
    index_path: str
    index_exists: bool
    index_updated_at: str = ""
    index_module_count: int = Field(default=0, ge=0)
    thresholds: dict[str, int] = Field(default_factory=dict)
    rows: tuple[ModuleReadinessRow, ...] = ()


class ModuleLibraryRow(StrictReadModel):
    module_id: str
    product: str = ""
    product_key: str = ""
    role: str = ""
    source_date: str = ""
    source_video: str = ""
    duration: float = Field(default=0.0, ge=0)
    confidence: float = Field(default=0.0, ge=0)
    quality_status: str = ""
    review_status: str = ""
    boundary_mode: str = ""
    visual_validation_status: str = "not_run"
    visual_product_hits: int = Field(default=0, ge=0)
    visual_product_confidence_max: float = Field(default=0.0, ge=0)
    visual_validation_reason: str = ""
    file_artifact: ArtifactRef | None = None
    transcript_text: str = ""


class ModuleLibraryPage(StrictReadModel):
    library_dir: str
    rows: tuple[ModuleLibraryRow, ...] = ()
    total: int = Field(default=0, ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)
    filter_options: dict[str, tuple[str, ...]] = Field(default_factory=dict)


class SettingsReadEntry(StrictReadModel):
    name: str
    value: bool | int | float | str | None
    source: str
    value_type: str
    category: str
    minimum: float | None = None
    maximum: float | None = None


class SettingsReadSnapshot(StrictReadModel):
    revision: str
    groups: dict[str, tuple[SettingsReadEntry, ...]] = Field(default_factory=dict)


class LogLine(StrictReadModel):
    line_number: int = Field(ge=1)
    text: str


class LogTail(StrictReadModel):
    path: str
    exists: bool
    total_lines: int = Field(default=0, ge=0)
    returned_lines: int = Field(default=0, ge=0)
    lines: tuple[LogLine, ...] = ()


class SystemStats(StrictReadModel):
    cpu_percent: float | None = None
    ram_percent: float | None = None
    ram_label: str = "Unavailable"
    disk_percent: float | None = None
    disk_label: str = "Unavailable"
    gpu_percent: float | None = None
    gpu_mem_percent: float | None = None
    gpu_label: str = "Unavailable"
