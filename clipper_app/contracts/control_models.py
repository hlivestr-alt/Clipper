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


class ControlJob(StrictModel):
    job_id: str
    operation: ControlOperation
    status: ControlJobStatus
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None
    request: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: str | None = None
    conflict_key: str | None = None
    actor: str = "operator"


class ControlJobPage(StrictModel):
    jobs: tuple[ControlJob, ...] = ()
    total: int = Field(default=0, ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


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
    control_path: str | None = None
    forever_state_path: str | None = None
    queue_state_path: str | None = None
    launch_config: QueueLaunchConfig | None = None
    actor: str = "operator"


class SettingsOverrideWriteRequest(StrictModel):
    overrides: dict[str, bool | int | float | str | None] = Field(default_factory=dict)
    expected_revision: str | None = None
    actor: str = "operator"


class SettingsOverrideDeleteRequest(StrictModel):
    expected_revision: str | None = None
    actor: str = "operator"


class RescoreRequest(StrictModel):
    output_dir: str
    working_dir: str | None = None
    limit: int | None = Field(default=None, ge=1)
    include_failed: bool = False
    force_rescore: bool = False
    flush_every: int | None = Field(default=None, ge=1)
    actor: str = "operator"


class ComplianceScanRequest(StrictModel):
    output_dir: str
    working_dir: str | None = None
    force: bool = True
    actor: str = "operator"


class ModuleAssemblyRequest(StrictModel):
    assembly_date: str | None = None
    product: str | None = None
    module_assembly_limit: int | None = Field(default=None, ge=0)
    module_product_zoom: bool = False
    actor: str = "operator"


class ExportBatchesRequest(StrictModel):
    output_root: str | None = None
    batch_size: int | None = Field(default=None, ge=1)
    dry_run: bool = False
    actor: str = "operator"


class ModuleReviewRequest(StrictModel):
    status: str
    note: str = ""
    reviewer: str = "operator"
    actor: str = "operator"


class VariationProfileWriteRequest(StrictModel):
    profile: dict[str, Any] = Field(default_factory=dict)
    expected_revision: str | None = None
    actor: str = "operator"


class VariationPreviewRequest(StrictModel):
    profile: dict[str, Any] = Field(default_factory=dict)
    variant_index: int | None = Field(default=None, ge=0)
    actor: str = "operator"


class VariationPresetWriteRequest(StrictModel):
    name: str
    profile: dict[str, Any] = Field(default_factory=dict)
    actor: str = "operator"
