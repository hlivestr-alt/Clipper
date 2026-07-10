from __future__ import annotations

from typing import Any, Callable

from clipper_app.adapters import (
    LegacyPipelineAdapter,
    LegacyQueueRunnerAdapter,
    LegacyQueueSupervisorAdapter,
)
from clipper_app.application.services import (
    ComplianceService,
    ExportPackagingService,
    HealthService,
    ModuleService,
    PipelineService,
    QueueControlService,
    QueueService,
    QueueSupervisorService,
    ScoringService,
)
from clipper_app.application.control_services import ControlJobService, SettingsService
from clipper_app.application.read_services import ReadDashboardService
from clipper_app.application.settings import LegacyConfigProvider
from clipper_app.contracts.models import QueueRunCommand


def build_pipeline_service(executor: Callable[..., dict[str, Any]]) -> PipelineService:
    return PipelineService(
        executor=LegacyPipelineAdapter(executor),
        settings_provider=LegacyConfigProvider(),
    )


def build_queue_service(runner_factory: Callable[[QueueRunCommand], Any]) -> QueueService:
    return QueueService(runner_factory=LegacyQueueRunnerAdapter(runner_factory))


def build_queue_supervisor_service(executor: Callable[..., int]) -> QueueSupervisorService:
    return QueueSupervisorService(executor=LegacyQueueSupervisorAdapter(executor))


def build_queue_control_service() -> QueueControlService:
    return QueueControlService(LegacyConfigProvider())


def build_scoring_service() -> ScoringService:
    return ScoringService()


def build_compliance_service() -> ComplianceService:
    return ComplianceService()


def build_export_packaging_service() -> ExportPackagingService:
    return ExportPackagingService()


def build_module_service() -> ModuleService:
    return ModuleService()


def build_health_service() -> HealthService:
    return HealthService()


def build_read_dashboard_service() -> ReadDashboardService:
    return ReadDashboardService(LegacyConfigProvider())


def build_settings_service() -> SettingsService:
    return SettingsService(LegacyConfigProvider())


def build_control_job_service(*, run_async: bool = True) -> ControlJobService:
    return ControlJobService(run_async=run_async)
