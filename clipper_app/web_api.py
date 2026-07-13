from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from clipper_app.application.control_services import (
    ControlJobService,
    JobCapacityError,
    JobConflictError,
    JobResultExpiredError,
    JobResultNotFoundError,
    SettingsRevisionConflict,
    SettingsService,
)
from clipper_app.application.api_security import ApiSecuritySettings, origin_allowed, requires_control_auth
from clipper_app.application.read_services import ReadDashboardService, ReadServiceResult
from clipper_app.application.services import (
    ComplianceService,
    ExportPackagingService,
    ModuleService,
    QueueControlService,
    ScoringService,
)
from clipper_app.application.settings import BROWSER_EDITABLE_SETTINGS, SETTINGS_REGISTRY
from clipper_app.contracts.control_models import (
    ComplianceScanRequest,
    ControlJob,
    ControlOperation,
    ExportBatchesRequest,
    ModuleAssemblyRequest,
    ModuleReviewRequest,
    QueueControlRequest,
    RescoreRequest,
    SettingsOverrideDeleteRequest,
    SettingsOverrideWriteRequest,
    VariationPresetWriteRequest,
    VariationPreviewRequest,
    VariationProfileWriteRequest,
)
from clipper_app.contracts.models import (
    ComplianceScanCommand,
    ExportPackagingCommand,
    ModuleAssemblyCommand,
    ModuleReviewCommand,
    QueueAction,
    QueueControlCommand,
    QueueLaunchConfig,
    ScoringCommand,
)
from clipper_app.contracts.read_models import SettingsReadEntry, SettingsReadSnapshot

try:
    from fastapi import FastAPI, HTTPException, Query, Request, Response, status
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
    from starlette.middleware.trustedhost import TrustedHostMiddleware
except ImportError as exc:  # pragma: no cover - exercised only when runtime deps are missing.
    raise RuntimeError(
        "FastAPI is required for the control app. Install requirements.txt first."
    ) from exc


def _envelope(result: ReadServiceResult) -> dict[str, Any]:
    data = result.data.model_dump(mode="json") if hasattr(result.data, "model_dump") else result.data
    return {
        "data": data,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_signatures": [signature.model_dump(mode="json") for signature in result.source_signatures],
        "warnings": list(result.warnings),
    }


def _read_response(result: ReadServiceResult, request: Request) -> Response:
    payload = _envelope(result)
    data = result.data
    revision = str(getattr(data, "revision", "") or "")
    signature_payload = [
        (item.path, item.exists, item.mtime_ns, item.size)
        for item in result.source_signatures
    ]
    if not revision and not signature_payload:
        revision = json.dumps(payload["data"], sort_keys=True, ensure_ascii=False, default=str)
    raw = json.dumps(
        {"revision": revision, "signatures": signature_payload, "query": sorted(request.query_params.multi_items())},
        sort_keys=True,
        ensure_ascii=False,
    )
    etag = f'"{hashlib.sha256(raw.encode("utf-8")).hexdigest()}"'
    headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    return JSONResponse(payload, headers=headers)


def _direction(direction: str) -> str:
    value = str(direction or "desc").casefold()
    if value not in {"asc", "desc"}:
        raise HTTPException(status_code=400, detail="direction must be asc or desc")
    return value


def _output_dir_or_404(service: ReadDashboardService, output_dir: str) -> str:
    path = Path(output_dir)
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    else:
        path = path.resolve()
    output_root = Path(getattr(service.cfg, "OUTPUT_DIR", r"D:\output_clips")).resolve()
    try:
        path.relative_to(output_root)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="output_dir is outside OUTPUT_DIR") from exc
    if not path.exists() or not path.is_dir():
        raise HTTPException(status_code=404, detail="output_dir was not found")
    return str(path)


def _output_root_or_404(service: ReadDashboardService, output_root: str | None) -> str:
    output_root_path = Path(getattr(service.cfg, "OUTPUT_DIR", r"D:\output_clips")).resolve()
    path = Path(output_root) if output_root else output_root_path
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    else:
        path = path.resolve()
    try:
        path.relative_to(output_root_path)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="output_root is outside OUTPUT_DIR") from exc
    if not path.exists() or not path.is_dir():
        raise HTTPException(status_code=404, detail="output_root was not found")
    return str(path)


def _settings_read_snapshot(settings_service: SettingsService) -> SettingsReadSnapshot:
    snapshot = settings_service.effective_snapshot()
    entries_by_name = {entry.name: entry for entry in snapshot.entries}
    groups: dict[str, list[SettingsReadEntry]] = {}
    for name, definition in sorted(SETTINGS_REGISTRY.items()):
        entry = entries_by_name.get(name)
        if entry is None:
            continue
        groups.setdefault(definition.category, []).append(
            SettingsReadEntry(
                name=name,
                value=entry.value,
                source=entry.source,
                value_type=definition.value_type.__name__,
                category=definition.category,
                minimum=definition.minimum,
                maximum=definition.maximum,
                editable=name in BROWSER_EDITABLE_SETTINGS,
                read_only_reason="" if name in BROWSER_EDITABLE_SETTINGS else "Operator-managed; restart required.",
            )
        )
    return SettingsReadSnapshot(
        revision=snapshot.revision,
        groups={key: tuple(value) for key, value in sorted(groups.items())},
    )


def _safe_module_identifier(module_id: str) -> str:
    value = str(module_id or "").strip()
    if not value or "\x00" in value or ":" in value or "/" in value or "\\" in value:
        raise HTTPException(status_code=400, detail="module_id must be an indexed module identifier, not a path")
    return value


def _validated_queue_launch_config(
    service: ReadDashboardService,
    request: QueueControlRequest,
) -> QueueLaunchConfig | None:
    launch = request.launch_config
    if launch is None:
        return None
    if request.action != QueueAction.START:
        raise HTTPException(status_code=400, detail="launch_config is only valid with action=start")
    if launch.run_mode.value != "single_video":
        return launch

    try:
        from video_queue import VIDEO_EXTS
    except Exception:
        VIDEO_EXTS = {".mp4", ".mkv", ".mov"}

    input_dir = Path(str(getattr(service.cfg, "QUEUE_INPUT_DIR", r"D:\VOD") or r"D:\VOD"))
    if not input_dir.is_absolute():
        input_dir = (Path.cwd() / input_dir).resolve()
    else:
        input_dir = input_dir.resolve()
    target = Path(str(launch.video_path or ""))
    if not target.is_absolute():
        target = input_dir / target
    target = target.resolve()
    try:
        target.relative_to(input_dir)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="video_path must be inside QUEUE_INPUT_DIR") from exc
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=400, detail="video_path was not found")
    if target.suffix.casefold() not in {suffix.casefold() for suffix in VIDEO_EXTS}:
        raise HTTPException(status_code=400, detail="video_path is not a supported VOD file")
    return launch.model_copy(update={"video_path": str(target)})


def _job_envelope(job: ControlJob, response: Response) -> dict[str, Any]:
    response.status_code = status.HTTP_202_ACCEPTED
    return _envelope(ReadServiceResult(job))


def _execute_with_invalidation(
    read_service: ReadDashboardService,
    domains: tuple[str, ...],
    execute: Callable[[], Any],
) -> Any:
    try:
        return execute()
    finally:
        read_service.invalidate(*domains)


def _conflict_response(exc: JobConflictError) -> HTTPException:
    detail: dict[str, Any] = {"message": str(exc)}
    if exc.conflicting_job_id:
        detail["conflicting_job_id"] = exc.conflicting_job_id
    if exc.job is not None:
        detail["job"] = exc.job.model_dump(mode="json")
    return HTTPException(status_code=409, detail=detail)


def _capacity_response(exc: JobCapacityError) -> HTTPException:
    detail: dict[str, Any] = {"message": str(exc), "lane": exc.lane}
    if exc.job is not None:
        detail["job"] = exc.job.model_dump(mode="json")
    return HTTPException(
        status_code=429,
        detail=detail,
        headers={"Retry-After": str(exc.retry_after)},
    )


def create_app(
    service: ReadDashboardService | None = None,
    *,
    job_service: ControlJobService | None = None,
    settings_service: SettingsService | None = None,
    queue_control_service: QueueControlService | None = None,
    scoring_service: ScoringService | None = None,
    compliance_service: ComplianceService | None = None,
    module_service: ModuleService | None = None,
    export_service: ExportPackagingService | None = None,
    security_settings: ApiSecuritySettings | None = None,
) -> FastAPI:
    read_service = service or ReadDashboardService()
    provider = read_service.settings_provider
    migrate_legacy_jobs = os.getenv("CLIPPER_MIGRATE_JOB_STORAGE", "").strip().casefold() in {
        "1",
        "true",
        "yes",
    }
    jobs = job_service or ControlJobService(
        read_service.cfg,
        auto_migrate_legacy=migrate_legacy_jobs,
    )
    settings_writer = settings_service or SettingsService(provider)
    queue_controls = queue_control_service or QueueControlService(provider)
    scorer = scoring_service or ScoringService(provider)
    compliance_runner = compliance_service or ComplianceService(provider)
    modules = module_service or ModuleService(provider)
    exporter = export_service or ExportPackagingService(provider)
    security = security_settings or ApiSecuritySettings.from_environment()
    if security.desktop and security.token is None:
        raise RuntimeError("CLIPPER_DESKTOP=1 requires CLIPPER_CONTROL_TOKEN")
    api = FastAPI(
        title="Clipper",
        version="0.3.0",
        description="Control API for queue, score, compliance, module, log, settings, and artifact visibility.",
    )
    api.add_middleware(
        CORSMiddleware,
        allow_origins=list(security.allowed_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "If-None-Match"],
    )
    api.add_middleware(TrustedHostMiddleware, allowed_hosts=list(security.allowed_hosts))

    @api.middleware("http")
    async def enforce_control_boundary(request: Request, call_next):
        if not origin_allowed(
            request.headers.get("origin"), request.headers.get("host", ""), security
        ):
            return JSONResponse({"detail": "Origin is not allowed"}, status_code=403)
        if requires_control_auth(request.method, request.url.path):
            if security.token is None:
                return JSONResponse({"detail": "Control authentication is not configured"}, status_code=503)
            if not security.authorize(request.headers.get("authorization")):
                return JSONResponse(
                    {"detail": "Valid control credentials are required"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
            request.state.actor = security.actor
        else:
            request.state.actor = "local-operator"
        return await call_next(request)

    @api.get("/api/health")
    def health() -> dict[str, Any]:
        return _envelope(ReadServiceResult({"status": "ok", "mode": "control"}))

    @api.get("/api/dashboard")
    def dashboard(request: Request) -> Response:
        if "state_path" in request.query_params:
            raise HTTPException(status_code=400, detail="state_path overrides are not supported")
        return _read_response(read_service.dashboard(), request)

    @api.get("/api/queue")
    def queue(request: Request) -> Response:
        if "state_path" in request.query_params:
            raise HTTPException(status_code=400, detail="state_path overrides are not supported")
        return _read_response(read_service.queue_detail(), request)

    @api.get("/api/queue/vods")
    def queue_vods() -> dict[str, Any]:
        return _envelope(read_service.queue_vods())

    @api.get("/api/scores")
    def scores(
        request: Request,
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        search: str | None = None,
        status: str | None = None,
        product: str | None = None,
        sort: str = "scored_at",
        direction: str = "desc",
    ) -> Response:
        try:
            result = read_service.scores(
                limit=limit,
                offset=offset,
                search=search,
                status=status,
                product=product,
                sort=sort,
                direction=_direction(direction),  # type: ignore[arg-type]
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _read_response(result, request)

    @api.get("/api/scores/{score_key}")
    def score_detail(score_key: str) -> dict[str, Any]:
        result = read_service.score_detail(score_key)
        if result.data.selected is None:
            raise HTTPException(status_code=404, detail="score_key was not found")
        return _envelope(result)

    @api.get("/api/compliance")
    def compliance(
        request: Request,
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        search: str | None = None,
        status: str | None = None,
        product: str | None = None,
        sort: str = "checked_at",
        direction: str = "desc",
    ) -> Response:
        try:
            result = read_service.compliance(
                limit=limit,
                offset=offset,
                search=search,
                status=status,
                product=product,
                sort=sort,
                direction=_direction(direction),  # type: ignore[arg-type]
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _read_response(result, request)

    @api.get("/api/compliance/detail")
    def compliance_detail(output_dir: str) -> dict[str, Any]:
        return _envelope(read_service.compliance_detail(_output_dir_or_404(read_service, output_dir)))

    @api.get("/api/modules/readiness")
    def module_readiness(request: Request) -> Response:
        return _read_response(read_service.module_readiness(), request)

    @api.get("/api/modules/library")
    def module_library(
        request: Request,
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        search: str | None = None,
        status: str | None = None,
        quality_status: str | None = None,
        review_status: str | None = None,
        visual_status: str | None = None,
        product: str | None = None,
        sort: str = "product",
        direction: str = "asc",
    ) -> Response:
        try:
            result = read_service.module_library(
                limit=limit,
                offset=offset,
                search=search,
                status=status,
                quality_status=quality_status,
                review_status=review_status,
                visual_status=visual_status,
                product=product,
                sort=sort,
                direction=_direction(direction),  # type: ignore[arg-type]
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _read_response(result, request)

    @api.get("/api/modules/{module_id}")
    def module_detail(module_id: str, request: Request) -> Response:
        result = read_service.module_detail(_safe_module_identifier(module_id))
        if result.data.selected is None:
            raise HTTPException(status_code=404, detail="module_id was not found")
        return _read_response(result, request)

    @api.get("/api/overview")
    def overview(request: Request) -> Response:
        return _read_response(read_service.overview(), request)

    @api.get("/api/logs")
    def logs(lines: int = Query(default=200, ge=1, le=1000)) -> dict[str, Any]:
        return _envelope(read_service.log_tail(lines=lines))

    @api.get("/api/settings")
    def settings() -> dict[str, Any]:
        return _envelope(read_service.settings_snapshot())

    @api.get("/api/settings/effective")
    def settings_effective() -> dict[str, Any]:
        return _envelope(ReadServiceResult(_settings_read_snapshot(settings_writer)))

    @api.get("/api/variations")
    def variations() -> dict[str, Any]:
        try:
            from variation_profile import load_active_profile, variation_options

            profile = load_active_profile(read_service.cfg)
            payload = {"profile": profile, **variation_options(read_service.cfg)}
            return _envelope(ReadServiceResult(payload))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @api.put("/api/variations")
    def variation_save(request: VariationProfileWriteRequest) -> dict[str, Any]:
        try:
            from variation_profile import VariationRevisionConflict, save_active_profile, variation_options

            profile = save_active_profile(
                read_service.cfg,
                request.profile,
                expected_revision=request.expected_revision,
            )
            payload = {"profile": profile, **variation_options(read_service.cfg)}
            return _envelope(ReadServiceResult(payload))
        except VariationRevisionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @api.post("/api/variations/previews")
    def variation_previews(request: VariationPreviewRequest) -> dict[str, Any]:
        try:
            from variation_profile import generate_previews

            return _envelope(ReadServiceResult(generate_previews(
                read_service.cfg,
                request.profile,
                variant_index=request.variant_index,
            )))
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @api.post("/api/variations/presets")
    def variation_preset_save(request: VariationPresetWriteRequest) -> dict[str, Any]:
        try:
            from variation_profile import save_preset

            return _envelope(ReadServiceResult(save_preset(read_service.cfg, request.name, request.profile)))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @api.get("/api/variations/presets/{preset_id}")
    def variation_preset(preset_id: str) -> dict[str, Any]:
        try:
            from variation_profile import load_preset

            return _envelope(ReadServiceResult(load_preset(read_service.cfg, preset_id)))
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @api.put("/api/settings/overrides")
    def settings_overrides(request: SettingsOverrideWriteRequest, response: Response, http_request: Request) -> dict[str, Any]:
        def execute() -> SettingsReadSnapshot:
            def update() -> SettingsReadSnapshot:
                snapshot = settings_writer.update(
                    request.overrides,
                    expected_revision=request.expected_revision,
                )
                return _settings_read_snapshot(settings_writer).model_copy(update={"revision": snapshot.revision})

            return _execute_with_invalidation(read_service, ("settings",), update)

        try:
            job = jobs.submit(
                operation=ControlOperation.SETTINGS_UPDATE,
                request=request,
                executor=execute,
                actor=http_request.state.actor,
            )
        except SettingsRevisionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except JobCapacityError as exc:
            raise _capacity_response(exc) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _job_envelope(job, response)

    @api.delete("/api/settings/overrides/{name}")
    def settings_override_delete(
        name: str,
        response: Response,
        http_request: Request,
        expected_revision: str | None = None,
    ) -> dict[str, Any]:
        request = SettingsOverrideDeleteRequest(expected_revision=expected_revision)

        def execute() -> SettingsReadSnapshot:
            def delete() -> SettingsReadSnapshot:
                snapshot = settings_writer.delete(name, expected_revision=request.expected_revision)
                return _settings_read_snapshot(settings_writer).model_copy(update={"revision": snapshot.revision})

            return _execute_with_invalidation(read_service, ("settings",), delete)

        try:
            job = jobs.submit(
                operation=ControlOperation.SETTINGS_DELETE,
                request={"name": name, **request.model_dump(mode="json")},
                executor=execute,
                actor=http_request.state.actor,
            )
        except SettingsRevisionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except JobCapacityError as exc:
            raise _capacity_response(exc) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _job_envelope(job, response)

    @api.post("/api/control/queue")
    def control_queue(request: QueueControlRequest, response: Response, http_request: Request) -> dict[str, Any]:
        launch_config = _validated_queue_launch_config(read_service, request)
        queue_cfg = provider.runtime_view(provider.snapshot())
        command = QueueControlCommand(
            action=request.action,
            control_path=str(
                getattr(queue_cfg, "QUEUE_CONTROL_FILE", "working/queue_control.json")
                or "working/queue_control.json"
            ),
            forever_state_path=str(
                getattr(queue_cfg, "QUEUE_FOREVER_STATE_FILE", "working/queue_forever_state.json")
                or "working/queue_forever_state.json"
            ),
            queue_state_path=str(
                getattr(queue_cfg, "QUEUE_STATE_FILE", "working/video_queue_state.json")
                or "working/video_queue_state.json"
            ),
            launch_config=launch_config,
        )
        try:
            job = jobs.submit(
                operation=ControlOperation.QUEUE_CONTROL,
                request=request,
                executor=lambda: _execute_with_invalidation(
                    read_service, ("queue",), lambda: queue_controls.execute(command)
                ),
                actor=http_request.state.actor,
                conflict_key="queue_control",
            )
        except JobConflictError as exc:
            raise _conflict_response(exc) from exc
        except JobCapacityError as exc:
            raise _capacity_response(exc) from exc
        return _job_envelope(job, response)

    @api.get("/api/control/jobs")
    def control_jobs(
        request: Request,
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        operation: str | None = None,
        status: str | None = None,
        actor: str | None = None,
    ) -> Response:
        return _read_response(ReadServiceResult(jobs.list(
            limit=limit,
            offset=offset,
            operation=operation,
            status=status,
            actor=actor,
        )), request)

    @api.get("/api/control/jobs/{job_id}")
    def control_job(job_id: str, request: Request, include_result: bool = True) -> Response:
        job = jobs.get(job_id, include_result=include_result)
        if job is None:
            raise HTTPException(status_code=404, detail="job_id was not found")
        return _read_response(ReadServiceResult(job), request)

    @api.get("/api/control/jobs/{job_id}/result-preview")
    def control_job_result_preview(job_id: str) -> dict[str, Any]:
        try:
            preview = jobs.get_result_preview(job_id)
        except JobResultExpiredError as exc:
            raise HTTPException(status_code=410, detail=str(exc)) from exc
        except JobResultNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return _envelope(ReadServiceResult(preview))

    @api.get("/api/control/jobs/{job_id}/result")
    def control_job_result(job_id: str) -> FileResponse:
        try:
            result_path = jobs.result_file(job_id)
        except JobResultExpiredError as exc:
            raise HTTPException(status_code=410, detail=str(exc)) from exc
        except JobResultNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return FileResponse(
            result_path,
            media_type="application/json",
            filename=f"clipper-job-{job_id}-result.json",
        )

    @api.post("/api/operations/rescore")
    def rescore(request: RescoreRequest, response: Response, http_request: Request) -> dict[str, Any]:
        output_dir = _output_dir_or_404(read_service, request.output_dir)
        command = ScoringCommand(
            output_dir=output_dir,
            working_dir=None,
            limit=request.limit,
            include_failed=request.include_failed,
            force_rescore=request.force_rescore,
            flush_every=request.flush_every,
        )
        try:
            job = jobs.submit(
                operation=ControlOperation.RESCORE,
                request=request.model_copy(update={"output_dir": output_dir}),
                executor=lambda: _execute_with_invalidation(
                    read_service, ("scores",), lambda: scorer.rescore(command)
                ),
                actor=http_request.state.actor,
                conflict_key=f"rescore:{output_dir.casefold()}",
            )
        except JobConflictError as exc:
            raise _conflict_response(exc) from exc
        except JobCapacityError as exc:
            raise _capacity_response(exc) from exc
        return _job_envelope(job, response)

    @api.post("/api/operations/compliance-scan")
    def compliance_scan(request: ComplianceScanRequest, response: Response, http_request: Request) -> dict[str, Any]:
        output_dir = _output_dir_or_404(read_service, request.output_dir)
        command = ComplianceScanCommand(
            output_dir=output_dir,
            working_dir=None,
            force=request.force,
        )
        try:
            job = jobs.submit(
                operation=ControlOperation.COMPLIANCE_SCAN,
                request=request.model_copy(update={"output_dir": output_dir}),
                executor=lambda: _execute_with_invalidation(
                    read_service, ("compliance", "scores"), lambda: compliance_runner.scan(command)
                ),
                actor=http_request.state.actor,
                conflict_key=f"compliance:{output_dir.casefold()}",
            )
        except JobConflictError as exc:
            raise _conflict_response(exc) from exc
        except JobCapacityError as exc:
            raise _capacity_response(exc) from exc
        return _job_envelope(job, response)

    @api.post("/api/operations/module-assembly")
    def module_assembly(request: ModuleAssemblyRequest, response: Response, http_request: Request) -> dict[str, Any]:
        command = ModuleAssemblyCommand(
            assembly_date=request.assembly_date,
            product=request.product,
            module_assembly_limit=request.module_assembly_limit,
            module_product_zoom=request.module_product_zoom,
        )
        try:
            job = jobs.submit(
                operation=ControlOperation.MODULE_ASSEMBLY,
                request=request,
                executor=lambda: _execute_with_invalidation(
                    read_service, ("modules",), lambda: modules.assemble(command)
                ),
                actor=http_request.state.actor,
                conflict_key="module_assembly",
            )
        except JobConflictError as exc:
            raise _conflict_response(exc) from exc
        except JobCapacityError as exc:
            raise _capacity_response(exc) from exc
        return _job_envelope(job, response)

    @api.post("/api/operations/export-batches")
    def export_batches(request: ExportBatchesRequest, response: Response, http_request: Request) -> dict[str, Any]:
        output_root = _output_root_or_404(read_service, request.output_root)
        command = ExportPackagingCommand(
            output_root=output_root,
            batch_size=request.batch_size,
            dry_run=request.dry_run,
        )
        try:
            job = jobs.submit(
                operation=ControlOperation.EXPORT_BATCHES,
                request=request.model_copy(update={"output_root": output_root}),
                executor=lambda: _execute_with_invalidation(
                    read_service, ("outputs",), lambda: exporter.package(command)
                ),
                actor=http_request.state.actor,
                conflict_key="export_batches",
            )
        except JobConflictError as exc:
            raise _conflict_response(exc) from exc
        except JobCapacityError as exc:
            raise _capacity_response(exc) from exc
        return _job_envelope(job, response)

    @api.post("/api/modules/{module_id}/review")
    def module_review(module_id: str, request: ModuleReviewRequest, response: Response, http_request: Request) -> dict[str, Any]:
        safe_module_id = _safe_module_identifier(module_id)
        command = ModuleReviewCommand(
            identifier=safe_module_id,
            status=request.status,
            note=request.note,
            reviewer=http_request.state.actor,
        )
        try:
            job = jobs.submit(
                operation=ControlOperation.MODULE_REVIEW,
                request={"module_id": safe_module_id, **request.model_dump(mode="json")},
                executor=lambda: _execute_with_invalidation(
                    read_service, ("modules",), lambda: modules.review(command)
                ),
                actor=http_request.state.actor,
            )
        except JobConflictError as exc:
            raise _conflict_response(exc) from exc
        except JobCapacityError as exc:
            raise _capacity_response(exc) from exc
        return _job_envelope(job, response)

    @api.get("/api/system")
    def system(request: Request) -> Response:
        return _read_response(read_service.system_stats(), request)

    @api.get("/api/artifacts")
    def artifacts(path: str) -> FileResponse:
        try:
            artifact = read_service.resolve_artifact(path)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return FileResponse(artifact.path, media_type=artifact.media_type)

    static_dir = Path(__file__).resolve().parent.parent / "new_app" / "dist"
    if static_dir.exists():
        assets_dir = static_dir / "assets"
        if assets_dir.exists():
            api.mount("/assets", StaticFiles(directory=assets_dir), name="new_app_assets")

        @api.get("/")
        @api.get("/{full_path:path}")
        def new_app(full_path: str = "") -> FileResponse:
            if full_path.startswith("api/"):
                raise HTTPException(status_code=404, detail="Not Found")
            requested = (static_dir / full_path).resolve() if full_path else static_dir / "index.html"
            try:
                requested.relative_to(static_dir.resolve())
            except ValueError as exc:
                raise HTTPException(status_code=404, detail="Not Found") from exc
            if requested.exists() and requested.is_file():
                return FileResponse(requested)
            return FileResponse(static_dir / "index.html")

    return api


app = create_app()
