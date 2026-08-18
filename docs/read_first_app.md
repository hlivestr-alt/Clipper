# Read and Control API

This document began as the Phase 2 “read-first” design. The same FastAPI + React application is now the production control surface, so the original read-only guarantees are historical rather than current.

## Architecture

- `clipper_app.application.read_services.ReadDashboardService` returns typed data from legacy artifacts or the feature-flagged SQLite catalog.
- `clipper_app.application.container.ApplicationServiceContainer` composes reads, settings, jobs, queue controls, scoring, compliance, modules, exports, and WhatsApp delivery.
- `clipper_app.web_api` exposes the services under `/api`, enforces auth/origin/host rules, serves artifacts safely, emits SSE invalidation, and serves the built SPA when available.
- `new_app/` contains the React/Vite/TypeScript operator app and Electron shell.

The default read authorities remain queue JSON, manifests, score summaries, compliance files, module indexes, logs, job files, variation/product-information files, and media artifacts. `CLIPPER_CATALOG_MODE=catalog` moves supported indexed reads to SQLite only after the rollout procedure.

Every JSON read uses this envelope:

```json
{
  "data": {},
  "generated_at": "2026-08-10T11:00:00+08:00",
  "source_signatures": [],
  "warnings": []
}
```

Read responses can include ETags/revisions. List endpoints use bounded pagination and validated filters/sorts. Unsupported sorts return `400`; explicit missing artifacts return `404`; paths outside allowed roots return `403`.

## Current Read Groups

- Health, catalog status, and `/api/events` live invalidation.
- Dashboard, overview, queue detail, and queue VOD discovery.
- Score index/detail and compliance index/detail.
- Module readiness, library, and detail.
- Logs, configured/effective settings, system statistics, and safe artifacts.
- Product-information status.
- Variation profile/options and presets.
- Trend page/media/patterns and TikTok OAuth status.
- Control-job list/detail/result preview/download.
- WhatsApp delivery status and Sheet outbox.

`clipper_app/web_api.py` is the authoritative route list.

## Mutations Added After the Read-First Phase

The application now supports queue start/continue/stop, settings overrides, rescore, compliance scans, module assembly/review, export packaging, product-information rescan, variation saves/previews/presets, TikTok OAuth and advertiser selection, trend refresh/download/analysis/media linking, and WhatsApp assignment/item/outbox transitions.

These actions use typed requests, path containment, revision/conflict checks, job/audit persistence where appropriate, and query/SSE invalidation. `config.py` is never written by the app.

## Authentication and Artifact Roots

All mutations and sensitive reads require `Authorization: Bearer <CLIPPER_CONTROL_TOKEN>`. Without a configured token they return `503`; invalid credentials return `401`. The TikTok callback endpoints are the deliberate unauthenticated exception.

Artifact serving is restricted to configured roots such as:

- `OUTPUT_DIR`.
- `WORKING_DIR`.
- `MODULE_LIBRARY_DIR`.
- approved trend media beneath `TREND_MEDIA_DIR` when returned through the normal artifact model.

Log tailing is bounded to 1,000 lines. User-supplied queue state and log paths are rejected by the web routes.

## Running Locally

```powershell
python -m pip install -r requirements.txt
.\run_new_app.ps1 -PnpmExe pnpm.cmd -InstallFrontendDeps
.\run_new_app.ps1 -PnpmExe pnpm.cmd
```

Default development URLs:

- React: `http://127.0.0.1:5173`.
- FastAPI: `http://127.0.0.1:8765`.

The launcher generates `CLIPPER_CONTROL_TOKEN`, sets an actor, enables legacy job-result migration, and shares the token with both the backend and Vite proxy. Running `pnpm.cmd dev` alone requires a matching token/backend environment for protected API calls.

For desktop operation, use [desktop_app.md](desktop_app.md). For mutation details, use [control_app.md](control_app.md). For storage modes, use [long_term_storage_rollout.md](long_term_storage_rollout.md).

## Remaining Boundaries

- No websocket layer is used; live invalidation is SSE with polling fallback.
- SQLite catalog and queue authority remain staged, not default.
- Database-backed control-job metadata is not implemented; jobs/results remain bounded files.
- The API has no social-publishing endpoint.
