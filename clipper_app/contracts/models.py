from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SettingEntry(StrictModel):
    name: str
    value: bool | int | float | str | None
    source: str = "legacy_config"


class SettingsSnapshot(StrictModel):
    entries: tuple[SettingEntry, ...]
    revision: str

    def get(self, name: str, default: Any = None) -> Any:
        for entry in self.entries:
            if entry.name == name:
                return entry.value
        return default

    def as_dict(self) -> dict[str, Any]:
        return {entry.name: entry.value for entry in self.entries}


class PipelineRunCommand(StrictModel):
    video_path: str
    skip_transcribe: bool = False
    skip_moments: bool = False
    skip_vision: bool = False
    cut_only: bool = False
    max_clips: int | None = Field(default=None, ge=1)
    min_score: float | None = Field(default=None, ge=0, le=10)
    force_rescore: bool = False
    extract_modules_only: bool = False
    force_modules: bool = False
    render_modules: bool = False
    modular_only: bool = False
    module_assembly_limit: int | None = Field(default=None, ge=0)
    module_product_zoom: bool = False
    output_tag: str | None = None
    working_tag: str | None = None
    control_path: str | None = None
    settings_overrides: dict[str, bool | int | float | str | None] = Field(default_factory=dict)


class PipelineResult(StrictModel):
    clips_created: int = Field(default=0, ge=0)
    clips_failed: int = Field(default=0, ge=0)
    clips_skipped: int = Field(default=0, ge=0)
    clips_blocked: int = Field(default=0, ge=0)
    moments_found: int = Field(default=0, ge=0)
    total_time: float = Field(default=0.0, ge=0)
    output_dir: str | None = None
    manifest_path: str | None = None
    scores_summary_path: str | None = None
    clips_scored: int = Field(default=0, ge=0)
    export_batches: dict[str, Any] = Field(default_factory=dict)
    module_extraction: dict[str, Any] = Field(default_factory=dict)
    modular_assembly: dict[str, Any] = Field(default_factory=dict)


class QueueRunMode(StrEnum):
    SINGLE_VIDEO = "single_video"
    FOLDER_ONCE = "folder_once"
    FOLDER_REPEAT = "folder_repeat"


class QueuePipelineMode(StrEnum):
    FULL = "full"
    CLIPS_ONLY = "clips_only"
    MODULES_ONLY = "modules_only"
    RAW_CUTS_ONLY = "raw_cuts_only"


class QueueVariantMode(StrEnum):
    ALL = "all"
    ORIGINAL = "original"
    CUSTOM = "custom"


class QueueLaunchConfig(StrictModel):
    run_mode: QueueRunMode = QueueRunMode.FOLDER_REPEAT
    pipeline_mode: QueuePipelineMode = QueuePipelineMode.FULL
    variant_mode: QueueVariantMode = QueueVariantMode.ALL
    variant_count: int = Field(default=1, ge=1, le=6)
    max_clips: int | None = Field(default=None, ge=0)
    video_path: str | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_and_validate(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        values = dict(data)
        run_mode = _enum_value(values.get("run_mode") or QueueRunMode.FOLDER_REPEAT)
        pipeline_mode = _enum_value(values.get("pipeline_mode") or QueuePipelineMode.FULL)
        variant_mode = _enum_value(values.get("variant_mode") or QueueVariantMode.ALL)
        video_path = str(values.get("video_path") or "").strip()
        if run_mode == QueueRunMode.SINGLE_VIDEO.value:
            if not video_path:
                raise ValueError("video_path is required for single_video run mode")
            values["video_path"] = video_path
        elif video_path:
            raise ValueError("video_path is only valid for single_video run mode")
        else:
            values["video_path"] = None

        if values.get("max_clips") == 0:
            values["max_clips"] = None

        if pipeline_mode == QueuePipelineMode.RAW_CUTS_ONLY.value:
            values["variant_mode"] = QueueVariantMode.ORIGINAL
            values["variant_count"] = 1
        elif variant_mode != QueueVariantMode.CUSTOM.value:
            values["variant_count"] = 1

        return values


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


class QueueRunCommand(StrictModel):
    input_dir: str
    state_path: str
    max_retries: int = Field(default=2, ge=0)
    max_inflight_videos: int = Field(default=1, ge=1)
    ffmpeg_max_parallel_clips: int | None = Field(default=None, ge=1)
    stage_admission_limit: int = Field(default=3, ge=1)
    max_clips: int | None = Field(default=None, ge=1)
    min_score: float | None = Field(default=None, ge=0, le=10)
    force_rescore: bool = False
    force_modules: bool = False
    output_tag: str | None = None
    working_tag: str | None = None
    poll_interval: float = Field(default=2.0, ge=0.5)
    scan_interval: float | None = Field(default=None, ge=0.5)
    stable_seconds: float | None = Field(default=None, ge=0)
    control_path: str | None = None
    yolo_in_subprocess: bool | None = None
    retry_failed: bool = False
    run_mode: QueueRunMode = QueueRunMode.FOLDER_REPEAT
    pipeline_mode: QueuePipelineMode = QueuePipelineMode.FULL
    variant_mode: QueueVariantMode = QueueVariantMode.ALL
    variant_count: int = Field(default=1, ge=1, le=6)
    video_path: str | None = None
    scan_once: bool = False
    settings_snapshot_file: str | None = None


class QueueRunResult(StrictModel):
    exit_code: int


class QueueSupervisorCommand(StrictModel):
    input_dir: str
    state_file: str
    forever_state_file: str
    control_file: str
    start_run_number: int = Field(default=1, ge=1)
    max_retries: int = Field(default=2, ge=0)
    max_inflight_videos: int = Field(default=1, ge=1)
    ffmpeg_max_parallel_clips: int = Field(default=2, ge=1)
    stage_admission_limit: int = Field(default=3, ge=1)
    poll_interval: float = Field(default=2.0, ge=0.5)
    scan_interval: float = Field(default=300.0, ge=0.5)
    stable_seconds: float = Field(default=60.0, ge=0)
    restart_delay_seconds: float = Field(default=30.0, ge=0)
    between_runs_delay_seconds: float = Field(default=10.0, ge=0)
    max_clips: int | None = Field(default=None, ge=1)
    min_score: float | None = Field(default=None, ge=0, le=10)
    python_exe: str
    force_rescore: bool = False
    force_modules: bool = False
    retry_failed: bool = False
    dry_run: bool = False
    run_mode: QueueRunMode = QueueRunMode.FOLDER_REPEAT
    pipeline_mode: QueuePipelineMode = QueuePipelineMode.FULL
    variant_mode: QueueVariantMode = QueueVariantMode.ALL
    variant_count: int = Field(default=1, ge=1, le=6)
    video_path: str | None = None
    settings_snapshot_file: str | None = None


class QueueAction(StrEnum):
    STATUS = "status"
    START = "start"
    CONTINUE = "continue"
    PAUSE = "pause"
    STOP = "stop"


class QueueControlCommand(StrictModel):
    action: QueueAction
    control_path: str | None = None
    forever_state_path: str | None = None
    queue_state_path: str | None = None
    launch_config: QueueLaunchConfig | None = None


class QueueSnapshot(StrictModel):
    control: dict[str, Any] = Field(default_factory=dict)
    supervisor: dict[str, Any] = Field(default_factory=dict)
    queue: dict[str, Any] = Field(default_factory=dict)


class ScoringCommand(StrictModel):
    output_dir: str
    working_dir: str | None = None
    limit: int | None = Field(default=None, ge=1)
    include_failed: bool = False
    force_rescore: bool = False
    flush_every: int | None = Field(default=None, ge=1)
    settings_overrides: dict[str, bool | int | float | str | None] = Field(default_factory=dict)


class ScoringResult(StrictModel):
    scores: tuple[dict[str, Any], ...] = ()


class ComplianceScanCommand(StrictModel):
    output_dir: str
    working_dir: str | None = None
    force: bool = True
    settings_overrides: dict[str, bool | int | float | str | None] = Field(default_factory=dict)


class ComplianceScanResult(StrictModel):
    output_dir: str
    manifest_path: str
    scanned: int = Field(ge=0)
    passed: int = Field(ge=0)
    blocked: int = Field(ge=0)
    auto_fixed: int = Field(default=0, ge=0)
    violation_count: int = Field(default=0, ge=0)


class ModuleAssemblyCommand(StrictModel):
    assembly_date: str | None = None
    product: str | None = None
    module_assembly_limit: int | None = Field(default=None, ge=0)
    module_product_zoom: bool = False


class ModuleValidationCommand(StrictModel):
    product: str | None = None
    limit: int | None = Field(default=None, ge=1)
    force: bool = False
    visual_status: str = "not_run"
    role: str | None = None
    approved_only: bool = False
    priority: str = "assembly_ready"


class ModuleReviewCommand(StrictModel):
    identifier: str
    status: str
    note: str = ""
    reviewer: str = "operator"


class ModuleReportCommand(StrictModel):
    include_library_report: bool = True
    include_review_queue: bool = False
    review_filter: str = "needs_review"
    review_limit: int | None = Field(default=None, ge=1)


class ModuleOperationResult(StrictModel):
    payload: dict[str, Any] = Field(default_factory=dict)


class ExportPackagingCommand(StrictModel):
    output_root: str | None = None
    batch_size: int | None = Field(default=None, ge=1)
    dry_run: bool = False
    settings_overrides: dict[str, bool | int | float | str | None] = Field(default_factory=dict)


class ExportPackagingResult(StrictModel):
    payload: dict[str, Any] = Field(default_factory=dict)
