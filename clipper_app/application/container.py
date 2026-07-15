from __future__ import annotations

from dataclasses import dataclass

from clipper_app.application.control_services import ControlJobService, SettingsService
from clipper_app.application.read_services import ReadDashboardService
from clipper_app.application.services import (
    ComplianceService,
    ExportPackagingService,
    ModuleService,
    QueueControlService,
    ScoringService,
)


@dataclass(frozen=True)
class ApplicationServiceContainer:
    reads: ReadDashboardService
    jobs: ControlJobService
    settings: SettingsService
    queue_controls: QueueControlService
    scoring: ScoringService
    compliance: ComplianceService
    modules: ModuleService
    exports: ExportPackagingService

    @classmethod
    def build(
        cls,
        reads: ReadDashboardService | None = None,
        *,
        jobs: ControlJobService | None = None,
        settings: SettingsService | None = None,
        queue_controls: QueueControlService | None = None,
        scoring: ScoringService | None = None,
        compliance: ComplianceService | None = None,
        modules: ModuleService | None = None,
        exports: ExportPackagingService | None = None,
        migrate_legacy_jobs: bool = False,
    ) -> "ApplicationServiceContainer":
        read_service = reads or ReadDashboardService()
        provider = read_service.settings_provider
        return cls(
            reads=read_service,
            jobs=jobs or ControlJobService(
                read_service.cfg,
                auto_migrate_legacy=migrate_legacy_jobs,
            ),
            settings=settings or SettingsService(provider),
            queue_controls=queue_controls or QueueControlService(provider),
            scoring=scoring or ScoringService(provider),
            compliance=compliance or ComplianceService(provider),
            modules=modules or ModuleService(provider),
            exports=exports or ExportPackagingService(provider),
        )
