from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import Field

from clipper_app.contracts.models import QueueAction, QueueLaunchConfig, StrictModel


class ControlJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    REJECTED = "rejected"


class ControlOperation(StrEnum):
    QUEUE_CONTROL = "queue_control"
    SETTINGS_UPDATE = "settings_update"
    SETTINGS_DELETE = "settings_delete"
    SETTINGS_RESET = "settings_reset"
    RESCORE = "rescore"
    COMPLIANCE_SCAN = "compliance_scan"
    MODULE_ASSEMBLY = "module_assembly"
    EXPORT_BATCHES = "export_batches"
    MODULE_REVIEW = "module_review"


class ControlJobResultSummary(StrictModel):
    eligible_count: int | None = Field(default=None, ge=0)
    actionable_count: int | None = Field(default=None, ge=0)
    packaged_count: int | None = Field(default=None, ge=0)
    pending_count: int | None = Field(default=None, ge=0)
    packaged_total: int | None = Field(default=None, ge=0)
    batch_size: int | None = Field(default=None, ge=0)
    dry_run: bool | None = None


class ControlJobResultMetadata(StrictModel):
    available: bool = False
    truncated: bool = False
    original_bytes: int = Field(default=0, ge=0)
    stored_bytes: int = Field(default=0, ge=0)
    expires_at: str | None = None


class ControlJob(StrictModel):
    schema_version: int = Field(default=2, ge=1)
    job_id: str
    operation: ControlOperation
    status: ControlJobStatus
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None
    request: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] | None = None
    result_summary: ControlJobResultSummary | None = None
    result_metadata: ControlJobResultMetadata = Field(default_factory=ControlJobResultMetadata)
    error: str | None = None
    conflict_key: str | None = None
    actor: str = "operator"


class ControlJobSummary(StrictModel):
    job_id: str
    operation: ControlOperation
    status: ControlJobStatus
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None
    result_summary: ControlJobResultSummary | None = None
    error: str | None = None
    conflict_key: str | None = None
    actor: str = "operator"


class ControlJobResultPreview(StrictModel):
    job_id: str
    preview: str
    truncated: bool = False
    original_bytes: int = Field(default=0, ge=0)
    stored_bytes: int = Field(default=0, ge=0)


class ControlJobPage(StrictModel):
    jobs: tuple[ControlJobSummary, ...] = ()
    total: int = Field(default=0, ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)
    active_count: int = Field(default=0, ge=0)


class ControlAuditEntry(StrictModel):
    job_id: str
    operation: ControlOperation
    status: ControlJobStatus
    timestamp: str
    message: str = ""
    actor: str = "operator"
    detail: dict[str, Any] = Field(default_factory=dict)


class QueueControlRequest(StrictModel):
    action: QueueAction
    launch_config: QueueLaunchConfig | None = None


class SettingsOverrideWriteRequest(StrictModel):
    overrides: dict[str, bool | int | float | str | None] = Field(default_factory=dict)
    expected_revision: str | None = None


class SettingsOverrideDeleteRequest(StrictModel):
    expected_revision: str | None = None


class RescoreRequest(StrictModel):
    output_dir: str
    limit: int | None = Field(default=None, ge=1)
    include_failed: bool = False
    force_rescore: bool = False
    flush_every: int | None = Field(default=None, ge=1)


class ComplianceScanRequest(StrictModel):
    output_dir: str
    force: bool = True


class ModuleAssemblyRequest(StrictModel):
    assembly_date: str | None = None
    product: str | None = None
    module_assembly_limit: int | None = Field(default=None, ge=0)
    module_product_zoom: bool = False


class ExportBatchesRequest(StrictModel):
    output_root: str | None = None
    batch_size: int | None = Field(default=None, ge=1)
    dry_run: bool = False


class ModuleReviewRequest(StrictModel):
    status: str
    note: str = ""


class VariationProfileWriteRequest(StrictModel):
    profile: dict[str, Any] = Field(default_factory=dict)
    expected_revision: str | None = None


class VariationPreviewRequest(StrictModel):
    profile: dict[str, Any] = Field(default_factory=dict)
    variant_index: int | None = Field(default=None, ge=0)


class VariationPresetWriteRequest(StrictModel):
    name: str
    profile: dict[str, Any] = Field(default_factory=dict)
