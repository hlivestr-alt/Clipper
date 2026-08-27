from __future__ import annotations

import hashlib
import html
import json
import mimetypes
import os
import asyncio
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs
from uuid import uuid4

import portalocker

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
from clipper_app.application.catalog import CatalogDatabase, CatalogIndexer, ChangeEventRepository
from clipper_app.application.container import ApplicationServiceContainer
from clipper_app.application.read_services import ReadDashboardService, ReadServiceResult
from clipper_app.application.trends import TrendService, TrendServiceError
from clipper_app.application.tiktok_oauth import TikTokOAuthError, TikTokOAuthService
from clipper_app.application.whatsapp_delivery import (
    WhatsAppConflict,
    WhatsAppDeliveryError,
    WhatsAppDeliveryService,
    WhatsAppNotFound,
)
from clipper_app.application.services import (
    ComplianceService,
    ExportPackagingService,
    QueueControlService,
    ScoringService,
)
from clipper_app.application.settings import BROWSER_EDITABLE_SETTINGS, SETTINGS_REGISTRY
from clipper_app.contracts.control_models import (
    ComplianceScanRequest,
    ControlJob,
    ControlOperation,
    ExportBatchesRequest,
    QueueControlRequest,
    RescoreRequest,
    SettingsOverrideDeleteRequest,
    SettingsOverrideWriteRequest,
    VariationPresetWriteRequest,
    VariationPreviewRequest,
    VariationProfileWriteRequest,
    TrendAnalysisRequest,
    TrendDownloadRequest,
    TrendMediaLinkRequest,
    TrendRefreshRequest,
    TikTokAdvertiserSelectionRequest,
)
from clipper_app.contracts.models import (
    ComplianceScanCommand,
    ExportPackagingCommand,
    QueueAction,
    QueueControlCommand,
    QueueLaunchConfig,
    ScoringCommand,
)
from clipper_app.contracts.read_models import SettingsReadEntry, SettingsReadSnapshot
from clipper_app.contracts.whatsapp_delivery_models import (
    WhatsAppAssignmentActionRequest,
    WhatsAppClaimRequest,
    WhatsAppDeliveryItemRequest,
    WhatsAppOutboxAckRequest,
)
from clipper_app.modular_scanner import ModularScannerService

try:
    from fastapi import FastAPI, HTTPException, Query, Request, Response, status
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
    from fastapi.staticfiles import StaticFiles
    from starlette.middleware.trustedhost import TrustedHostMiddleware
except ImportError as exc:  # pragma: no cover - exercised only when runtime deps are missing.
    raise RuntimeError(
        "FastAPI is required for the control app. Install requirements.txt first."
    ) from exc


class ImmutableStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: dict[str, Any]):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


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
    revision = str(result.revision or getattr(data, "revision", "") or "")
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


def _range_file_response(path: Path, request: Request) -> Response:
    size = path.stat().st_size
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    headers = {"Accept-Ranges": "bytes", "Content-Disposition": f'inline; filename="{path.name}"'}
    range_header = request.headers.get("range")
    if not range_header:
        if request.method == "HEAD":
            return Response(status_code=200, media_type=media_type, headers={**headers, "Content-Length": str(size)})
        return FileResponse(path, media_type=media_type, filename=path.name, content_disposition_type="inline", headers=headers)
    try:
        unit, value = range_header.strip().split("=", 1)
        if unit.casefold() != "bytes" or "," in value:
            raise ValueError
        start_text, end_text = value.split("-", 1)
        if not start_text:
            suffix = int(end_text)
            if suffix <= 0:
                raise ValueError
            start = max(0, size - suffix)
            end = size - 1
        else:
            start = int(start_text)
            end = int(end_text) if end_text else size - 1
        if start < 0 or end < start or start >= size:
            raise ValueError
        end = min(end, size - 1)
    except (ValueError, TypeError):
        return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"})
    length = end - start + 1
    partial_headers = {
        **headers,
        "Content-Range": f"bytes {start}-{end}/{size}",
        "Content-Length": str(length),
    }
    if request.method == "HEAD":
        return Response(status_code=206, media_type=media_type, headers=partial_headers)

    def stream():
        remaining = length
        with path.open("rb") as handle:
            handle.seek(start)
            while remaining:
                block = handle.read(min(1024 * 1024, remaining))
                if not block:
                    break
                remaining -= len(block)
                yield block

    return StreamingResponse(stream(), status_code=206, media_type=media_type, headers=partial_headers)


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


def _validated_queue_launch_config(
    service: ReadDashboardService,
    request: QueueControlRequest,
) -> QueueLaunchConfig | None:
    launch = request.launch_config
    if launch is None:
        return None
    if request.action != QueueAction.START:
        raise HTTPException(status_code=400, detail="launch_config is only valid with action=start")
    if launch.pipeline_mode.value == "modules_only":
        raise HTTPException(
            status_code=400,
            detail="Modules Only is a legacy unsupported pipeline mode and cannot be started.",
        )
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
        result = execute()
        catalog_indexer = getattr(read_service, "catalog_indexer", None)
        if catalog_indexer is not None and read_service.catalog_mode in {"shadow", "catalog"}:
            if set(domains) & {"outputs", "scores", "compliance"}:
                try:
                    catalog_indexer.backfill()
                except Exception as exc:
                    catalog_indexer.record_repair(
                        ",".join(sorted(domains)),
                        "post-mutation projection",
                        exc,
                    )
        return result
    finally:
        read_service.invalidate(*domains)
        event_repository = getattr(read_service, "change_events", None)
        if event_repository is not None:
            event_repository.publish(domains)


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


def _oauth_result_html(success: bool, detail: str) -> str:
    title = "TikTok connected" if success else "TikTok authorization failed"
    color = "#4ade80" if success else "#fb7185"
    return (
        "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
        "<meta http-equiv='Cache-Control' content='no-store'><title>" + html.escape(title) + "</title>"
        "<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0b0d12;color:#f4f6fa;"
        "font-family:Inter,Segoe UI,sans-serif}main{max-width:560px;margin:24px;padding:28px;border:1px solid #353c49;"
        "border-radius:14px;background:#11141a}h1{color:" + color + ";font-size:1.45rem}p{color:#b7bfcc;line-height:1.6}</style>"
        "</head><body><main><h1>" + html.escape(title) + "</h1><p>" + html.escape(detail) + "</p>"
        "<p>This window can now be closed.</p></main></body></html>"
    )


def create_app(
    service: ReadDashboardService | None = None,
    *,
    job_service: ControlJobService | None = None,
    settings_service: SettingsService | None = None,
    queue_control_service: QueueControlService | None = None,
    scoring_service: ScoringService | None = None,
    compliance_service: ComplianceService | None = None,
    export_service: ExportPackagingService | None = None,
    security_settings: ApiSecuritySettings | None = None,
    tiktok_oauth_service: TikTokOAuthService | None = None,
    whatsapp_delivery_service: WhatsAppDeliveryService | None = None,
    modular_scanner_service: ModularScannerService | None = None,
) -> FastAPI:
    migrate_legacy_jobs = os.getenv("CLIPPER_MIGRATE_JOB_STORAGE", "").strip().casefold() in {
        "1",
        "true",
        "yes",
    }
    container = ApplicationServiceContainer.build(
        service,
        jobs=job_service,
        settings=settings_service,
        queue_controls=queue_control_service,
        scoring=scoring_service,
        compliance=compliance_service,
        exports=export_service,
        whatsapp_delivery=whatsapp_delivery_service,
        migrate_legacy_jobs=migrate_legacy_jobs,
    )
    read_service = container.reads
    provider = read_service.settings_provider
    jobs = container.jobs
    settings_writer = container.settings
    queue_controls = container.queue_controls
    scorer = container.scoring
    compliance_runner = container.compliance
    exporter = container.exports
    whatsapp_delivery = container.whatsapp_delivery
    modular_scanner = modular_scanner_service or ModularScannerService(read_service.cfg)
    catalog = CatalogDatabase.from_config(read_service.cfg)
    tiktok_oauth = tiktok_oauth_service or TikTokOAuthService.from_environment(read_service.cfg)
    trends = TrendService(catalog, read_service.cfg, oauth_service=tiktok_oauth)
    change_events = ChangeEventRepository(catalog)
    read_service.change_events = change_events
    read_service.catalog_database = catalog
    read_service.catalog_indexer = CatalogIndexer(catalog, read_service.cfg)
    previous_job_change = jobs.on_change

    def publish_job_change(job: ControlJob) -> None:
        if previous_job_change is not None:
            previous_job_change(job)
        change_events.publish(("jobs",))

    jobs.on_change = publish_job_change
    security = security_settings or ApiSecuritySettings.from_environment()
    if security.desktop and security.token is None:
        raise RuntimeError("CLIPPER_DESKTOP=1 requires CLIPPER_CONTROL_TOKEN")
    api = FastAPI(
        title="Clipper",
        version="0.3.0",
        description="Control API for queue, score, compliance, log, settings, and artifact visibility.",
    )
    api.add_middleware(
        CORSMiddleware,
        allow_origins=list(security.allowed_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "If-None-Match"],
    )
    api.add_middleware(TrustedHostMiddleware, allowed_hosts=list(security.allowed_hosts))

    catalog_mode = os.getenv("CLIPPER_CATALOG_MODE", "legacy").strip().casefold() or "legacy"
    catalog_stop = threading.Event()
    signal_stop = threading.Event()
    api.state.sse_metrics = {
        "active_clients": 0,
        "peak_clients": 0,
        "events_sent": 0,
        "resets_sent": 0,
        "dropped_events": 0,
    }

    def start_signal_monitor() -> None:
        if os.getenv("CLIPPER_PUSH_INVALIDATION", "1").strip().casefold() in {"0", "false", "no"}:
            return

        configured = (
            ("logs", Path.cwd() / "pipeline.log"),
            ("queue", Path(str(getattr(read_service.cfg, "QUEUE_INPUT_DIR", "D:/VOD")))),
            ("queue", Path(str(getattr(read_service.cfg, "QUEUE_STATE_FILE", "working/video_queue_state.json")))),
            ("queue", Path(str(getattr(read_service.cfg, "QUEUE_CONTROL_FILE", "working/queue_control.json")))),
            ("queue", Path(str(getattr(read_service.cfg, "QUEUE_FOREVER_STATE_FILE", "working/queue_forever_state.json")))),
        )

        def signature(path: Path) -> tuple[int, int] | None:
            try:
                stat = path.stat()
                return stat.st_mtime_ns, stat.st_size
            except OSError:
                return None

        def run() -> None:
            previous = {(topic, str(path)): signature(path) for topic, path in configured}
            while not signal_stop.wait(1.0):
                changed: set[str] = set()
                for topic, path in configured:
                    key = (topic, str(path))
                    current = signature(path)
                    if current != previous.get(key):
                        previous[key] = current
                        changed.add(topic)
                if changed:
                    try:
                        change_events.publish(changed)
                    except Exception:
                        # Signal monitoring is advisory; canonical reads remain authoritative.
                        pass

        threading.Thread(target=run, name="clipper-signal-monitor", daemon=True).start()

    def stop_signal_monitor() -> None:
        signal_stop.set()

    if catalog_mode in {"shadow", "catalog"}:
        def start_catalog_indexer() -> None:
            api.state.catalog_backfill = {"status": "starting"}

            def run() -> None:
                lock_path = Path(f"{catalog.path}.index.lock")
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with portalocker.Lock(str(lock_path), mode="a", timeout=0.1):
                        while not catalog_stop.is_set():
                            started_at = time.monotonic()
                            try:
                                result = CatalogIndexer(catalog, read_service.cfg).backfill()
                                api.state.catalog_backfill = {
                                    "status": "ready",
                                    "duration_seconds": round(time.monotonic() - started_at, 3),
                                    **result,
                                }
                            except Exception as exc:
                                api.state.catalog_backfill = {"status": "error", "error": str(exc)}
                            catalog_stop.wait(300.0)
                except portalocker.exceptions.LockException:
                    api.state.catalog_backfill = {"status": "standby"}

            threading.Thread(target=run, name="clipper-catalog-indexer", daemon=True).start()

        def stop_catalog_indexer() -> None:
            catalog_stop.set()

    @asynccontextmanager
    async def lifespan(_application: FastAPI):
        signal_stop.clear()
        catalog_stop.clear()
        start_signal_monitor()
        if catalog_mode in {"shadow", "catalog"}:
            start_catalog_indexer()
        try:
            yield
        finally:
            stop_signal_monitor()
            modular_scanner.close()
            if catalog_mode in {"shadow", "catalog"}:
                stop_catalog_indexer()

    api.router.lifespan_context = lifespan

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

    @api.middleware("http")
    async def redact_oauth_callback_query(request: Request, call_next):
        normalized = request.url.path.rstrip("/") or "/"
        if normalized in {"/callback", "/api/integrations/tiktok/oauth/callback"}:
            raw = request.scope.get("query_string", b"").decode("utf-8", errors="replace")
            request.state.tiktok_oauth_callback = parse_qs(raw, keep_blank_values=True)
            request.scope["query_string"] = b""
        return await call_next(request)

    @api.get("/api/health")
    def health() -> dict[str, Any]:
        return _envelope(ReadServiceResult({"status": "ok", "mode": "control"}))

    @api.get("/api/modular-scanner/sources")
    def modular_sources() -> dict[str, Any]:
        return _envelope(ReadServiceResult({"sources": modular_scanner.discover()}))

    @api.post("/api/modular-scanner/scans")
    def modular_start_scan(payload: dict[str, Any]) -> dict[str, Any]:
        if set(payload) != {"source_id"} or not isinstance(payload.get("source_id"), str):
            raise HTTPException(status_code=422, detail="Body must contain only source_id")
        try:
            scan, reused = modular_scanner.start_scan(payload["source_id"])
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (PermissionError, FileNotFoundError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return _envelope(ReadServiceResult({"scan": scan, "reused": reused}))

    @api.post("/api/modular-scanner/sources/{source_id}/rescan")
    def modular_rescan(source_id: str) -> dict[str, Any]:
        try:
            scan, _ = modular_scanner.start_scan(source_id, rescan=True)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (PermissionError, FileNotFoundError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return _envelope(ReadServiceResult({"scan": scan, "reused": False}))

    @api.get("/api/modular-scanner/scans/{scan_id}")
    def modular_scan(scan_id: str) -> dict[str, Any]:
        try:
            return _envelope(ReadServiceResult({"scan": modular_scanner.get_scan(scan_id)}))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @api.get("/api/modular-scanner/scans")
    def modular_scan_history(source_id: str) -> dict[str, Any]:
        try:
            return _envelope(ReadServiceResult({"scans": modular_scanner.history(source_id)}))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @api.get("/api/modular-scanner/segments")
    def modular_segments(
        source_id: str,
        scan_id: str | None = None,
        product: str | None = None,
        role: str | None = None,
        minimum_confidence: float = Query(0.0, ge=0.0, le=1.0),
        search: str = Query("", max_length=200),
        sort: str = Query("timestamp", pattern="^(timestamp|duration|confidence)$"),
    ) -> dict[str, Any]:
        try:
            rows = modular_scanner.segments(
                source_id=source_id,
                scan_id=scan_id,
                product=product,
                role=role,
                minimum_confidence=minimum_confidence,
                search=search,
                sort=sort,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _envelope(ReadServiceResult({"segments": rows}))

    @api.get("/api/modular-scanner/media/{source_id}")
    def modular_media(source_id: str, request: Request) -> Response:
        try:
            path = modular_scanner.media_path(source_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _range_file_response(path, request)

    @api.head("/api/modular-scanner/media/{source_id}")
    def modular_media_head(source_id: str, request: Request) -> Response:
        return modular_media(source_id, request)

    def _delivery_call(callback: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        try:
            return _envelope(ReadServiceResult(callback()))
        except WhatsAppNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except WhatsAppConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (ValueError, WhatsAppDeliveryError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @api.get("/api/whatsapp-delivery/status")
    def whatsapp_delivery_status(limit: int = Query(default=100, ge=1, le=1000)) -> dict[str, Any]:
        return _delivery_call(lambda: whatsapp_delivery.status(limit=limit))

    @api.post("/api/whatsapp-delivery/claims")
    def whatsapp_delivery_claim(
        claim: WhatsAppClaimRequest, request: Request
    ) -> dict[str, Any]:
        return _delivery_call(
            lambda: whatsapp_delivery.claim(
                claim, actor=str(getattr(request.state, "actor", "local-operator"))
            )
        )

    @api.get("/api/whatsapp-delivery/sheet-outbox")
    def whatsapp_delivery_sheet_outbox(
        limit: int = Query(default=100, ge=1, le=1000)
    ) -> dict[str, Any]:
        return _delivery_call(lambda: whatsapp_delivery.pending_outbox(limit=limit))

    @api.post("/api/whatsapp-delivery/sheet-outbox/{outbox_id}/ack")
    def whatsapp_delivery_sheet_outbox_ack(
        outbox_id: str, payload: WhatsAppOutboxAckRequest
    ) -> dict[str, Any]:
        return _delivery_call(
            lambda: whatsapp_delivery.acknowledge_outbox(
                outbox_id, success=payload.success, error=payload.error
            )
        )

    @api.post("/api/whatsapp-delivery/assignments/{assignment_id}/{action}")
    def whatsapp_delivery_transition(
        assignment_id: str,
        action: str,
        payload: WhatsAppAssignmentActionRequest,
        request: Request,
    ) -> dict[str, Any]:
        targets = {
            "start": "sending",
            "sent": "sent",
            "fail": "delivery_failed",
            "cancel": "cancelled",
            "release": "unassigned",
            "retry": "sending",
        }
        target = targets.get(action.casefold())
        if target is None:
            raise HTTPException(status_code=400, detail="Unknown assignment action")
        return _delivery_call(
            lambda: whatsapp_delivery.transition(
                assignment_id,
                target,
                expected_version=payload.expected_version,
                actor=str(getattr(request.state, "actor", "local-operator")),
                idempotency_key=payload.idempotency_key,
                error=payload.error,
                drive_or_media_reference=payload.drive_or_media_reference,
                operator_reason=payload.operator_reason,
            )
        )

    @api.put("/api/whatsapp-delivery/assignments/{assignment_id}/items")
    def whatsapp_delivery_item(
        assignment_id: str, payload: WhatsAppDeliveryItemRequest
    ) -> dict[str, Any]:
        return _delivery_call(
            lambda: whatsapp_delivery.update_item(
                assignment_id,
                payload.relative_path,
                status=payload.status,
                whatsapp_media_id=payload.whatsapp_media_id,
                whatsapp_message_id=payload.whatsapp_message_id,
                drive_or_media_reference=payload.drive_or_media_reference,
                error=payload.error,
            )
        )

    @api.get("/api/integrations/tiktok/oauth/status")
    def tiktok_oauth_status() -> dict[str, Any]:
        return _envelope(ReadServiceResult(tiktok_oauth.status()))

    @api.post("/api/integrations/tiktok/oauth/start")
    def tiktok_oauth_start() -> dict[str, Any]:
        try:
            return _envelope(ReadServiceResult(tiktok_oauth.authorization_url()))
        except TikTokOAuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @api.put("/api/integrations/tiktok/oauth/advertiser")
    def tiktok_oauth_select_advertiser(request: TikTokAdvertiserSelectionRequest) -> dict[str, Any]:
        try:
            payload = tiktok_oauth.select_advertiser(request.advertiser_id)
            change_events.publish(("trends",))
            return _envelope(ReadServiceResult(payload))
        except TikTokOAuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @api.get("/callback", response_class=HTMLResponse)
    @api.get("/api/integrations/tiktok/oauth/callback", response_class=HTMLResponse)
    def tiktok_oauth_callback(http_request: Request) -> HTMLResponse:
        params = getattr(http_request.state, "tiktok_oauth_callback", {})
        error = str((params.get("error") or [""])[0])
        if error:
            return HTMLResponse(
                _oauth_result_html(False, "TikTok authorization was cancelled or rejected."),
                status_code=400,
                headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
            )
        auth_code = str((params.get("auth_code") or params.get("code") or [""])[0])
        state = str((params.get("state") or [""])[0])
        try:
            result = tiktok_oauth.exchange_callback(auth_code, state)
            change_events.publish(("trends",))
            selected = result.get("selected_advertiser_id")
            detail = "Authorization saved. You may return to Clipper."
            if not selected:
                detail = "Authorization saved. Return to Clipper and select an advertiser account."
            return HTMLResponse(
                _oauth_result_html(True, detail),
                headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
            )
        except TikTokOAuthError as exc:
            return HTMLResponse(
                _oauth_result_html(False, str(exc)),
                status_code=400,
                headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
            )

    @api.get("/api/catalog/status")
    def catalog_status() -> dict[str, Any]:
        status_payload = catalog.status()
        status_payload["backfill"] = getattr(api.state, "catalog_backfill", {"status": "disabled"})
        status_payload["sse"] = dict(api.state.sse_metrics)
        status_payload["queue_storage_mode"] = os.getenv(
            "CLIPPER_QUEUE_STORAGE_MODE", "json"
        ).strip().casefold() or "json"
        return _envelope(ReadServiceResult(status_payload, revision=json.dumps(status_payload["revisions"], sort_keys=True)))

    @api.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        if os.getenv("CLIPPER_PUSH_INVALIDATION", "1").strip().casefold() in {"0", "false", "no"}:
            raise HTTPException(status_code=404, detail="Push invalidation is disabled")
        async def stream():
            metrics = api.state.sse_metrics
            metrics["active_clients"] += 1
            metrics["peak_clients"] = max(metrics["peak_clients"], metrics["active_clients"])
            try:
                cursor = request.headers.get("last-event-id") or request.query_params.get("last_event_id")
                last_heartbeat = 0.0
                while not await request.is_disconnected():
                    reset, pending = change_events.after(cursor, limit=64)
                    if reset:
                        payload = {
                            "schema_version": 1,
                            "topics": ["*"],
                            "occurred_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                        }
                        cursor = None
                        metrics["resets_sent"] += 1
                        yield f"retry: 5000\nevent: reset\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"
                    elif pending:
                        topics = sorted({topic for event in pending for topic in event.topics})
                        revisions: dict[str, int] = {}
                        for event in pending:
                            revisions.update({key: int(value) for key, value in event.revisions.items()})
                        newest = pending[-1]
                        cursor = newest.event_id
                        payload = {
                            "schema_version": 1,
                            "topics": topics,
                            "revisions": revisions,
                            "occurred_at": newest.occurred_at,
                        }
                        metrics["events_sent"] += len(pending)
                        yield (
                            f"id: {cursor}\nretry: 5000\nevent: invalidate\n"
                            f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
                        )
                    else:
                        now = asyncio.get_running_loop().time()
                        if now - last_heartbeat >= 15.0:
                            last_heartbeat = now
                            yield ": heartbeat\n\n"
                    await asyncio.sleep(0.25)
            finally:
                metrics["active_clients"] = max(0, metrics["active_clients"] - 1)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    @api.get("/api/dashboard")
    def dashboard(request: Request) -> Response:
        if "state_path" in request.query_params:
            raise HTTPException(status_code=400, detail="state_path overrides are not supported")
        return _read_response(read_service.dashboard(), request)

    @api.get("/api/queue")
    def queue(
        request: Request,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> Response:
        if "state_path" in request.query_params:
            raise HTTPException(status_code=400, detail="state_path overrides are not supported")
        return _read_response(read_service.queue_detail(limit=limit, offset=offset), request)

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

    @api.get("/api/product-information")
    def product_information() -> dict[str, Any]:
        try:
            from product_information import product_information_status

            return _envelope(ReadServiceResult(product_information_status(read_service.cfg)))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @api.post("/api/product-information/rescan")
    def product_information_rescan() -> dict[str, Any]:
        try:
            from product_information import product_information_status, scan_product_information

            scan_product_information(read_service.cfg, force=True)
            return _envelope(ReadServiceResult(product_information_status(read_service.cfg)))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

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
                product_key=request.product_key,
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

    @api.get("/api/trends")
    def trend_page(
        country_code: str = Query(default="ID", min_length=2, max_length=2),
        date_range: str = Query(default="1DAY"),
        category_name: str = Query(default="BEAUTY_AND_PERSONAL_CARE"),
    ) -> dict[str, Any]:
        try:
            return _envelope(ReadServiceResult(trends.page(country_code, date_range, category_name)))
        except TrendServiceError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @api.get("/api/trends/media-files")
    def trend_media_files() -> dict[str, Any]:
        return _envelope(ReadServiceResult(trends.media_files()))

    @api.get("/api/trends/patterns/{pattern_id}")
    def trend_pattern(pattern_id: str) -> dict[str, Any]:
        try:
            return _envelope(ReadServiceResult(trends.pattern(pattern_id)))
        except TrendServiceError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @api.put("/api/trends/videos/{video_id}/media")
    def trend_media_link(
        video_id: str,
        request: TrendMediaLinkRequest,
        http_request: Request,
    ) -> dict[str, Any]:
        try:
            payload = trends.link_media(video_id, request.relative_path, http_request.state.actor)
            change_events.publish(("trends",))
            return _envelope(ReadServiceResult(payload))
        except TrendServiceError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @api.post("/api/operations/trend-refresh")
    def trend_refresh(request: TrendRefreshRequest, response: Response, http_request: Request) -> dict[str, Any]:
        try:
            job = jobs.submit(
                operation=ControlOperation.TREND_REFRESH,
                request=request,
                executor=lambda: _execute_with_invalidation(
                    read_service, ("trends",), lambda: trends.refresh(request)
                ),
                actor=http_request.state.actor,
                conflict_key=f"trend_refresh:{request.country_code.upper()}:{request.date_range.upper()}:{request.category_name.upper()}",
            )
        except JobConflictError as exc:
            raise _conflict_response(exc) from exc
        except JobCapacityError as exc:
            raise _capacity_response(exc) from exc
        except TrendServiceError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _job_envelope(job, response)

    @api.post("/api/operations/trend-download")
    def trend_download(request: TrendDownloadRequest, response: Response, http_request: Request) -> dict[str, Any]:
        if request.rights_confirmed is not True:
            raise HTTPException(
                status_code=400,
                detail="Permission to download and store these videos must be confirmed.",
            )
        run_id = uuid4().hex
        actor = http_request.state.actor
        try:
            job = jobs.submit(
                operation=ControlOperation.TREND_DOWNLOAD,
                request=request,
                executor=lambda: _execute_with_invalidation(
                    read_service,
                    ("trends",),
                    lambda: trends.download_all(request, run_id=run_id, actor=actor),
                ),
                actor=actor,
                conflict_key="trend_download",
            )
        except JobConflictError as exc:
            raise _conflict_response(exc) from exc
        except JobCapacityError as exc:
            raise _capacity_response(exc) from exc
        except TrendServiceError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _job_envelope(job, response)

    @api.post("/api/operations/trend-analysis")
    def trend_analysis(request: TrendAnalysisRequest, response: Response, http_request: Request) -> dict[str, Any]:
        video_key = hashlib.sha256("\n".join(sorted(set(request.video_ids))).encode("utf-8")).hexdigest()[:16]
        try:
            job = jobs.submit(
                operation=ControlOperation.TREND_ANALYSIS,
                request=request,
                executor=lambda: _execute_with_invalidation(
                    read_service, ("trends",), lambda: trends.analyze(request)
                ),
                actor=http_request.state.actor,
                conflict_key=f"trend_analysis:{request.snapshot_id}:{video_key}",
            )
        except JobConflictError as exc:
            raise _conflict_response(exc) from exc
        except JobCapacityError as exc:
            raise _capacity_response(exc) from exc
        except TrendServiceError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _job_envelope(job, response)

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

    configured_static_dir = os.getenv("CLIPPER_STATIC_DIR", "").strip()
    static_dir = (
        Path(configured_static_dir).expanduser().resolve()
        if configured_static_dir
        else Path(__file__).resolve().parent.parent / "new_app" / "dist"
    )
    if configured_static_dir and not (static_dir / "index.html").is_file():
        raise RuntimeError(f"CLIPPER_STATIC_DIR does not contain index.html: {static_dir}")
    if static_dir.exists():
        assets_dir = static_dir / "assets"
        if assets_dir.exists():
            api.mount("/assets", ImmutableStaticFiles(directory=assets_dir), name="new_app_assets")

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
                headers = {"Cache-Control": "private, no-cache"} if requested.name == "index.html" else {}
                return FileResponse(requested, headers=headers)
            return FileResponse(static_dir / "index.html", headers={"Cache-Control": "private, no-cache"})

    return api


app = create_app()
