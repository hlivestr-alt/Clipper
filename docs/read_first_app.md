# Read-First App

Phase 2 added the read-first FastAPI + React application for production
visibility. The same application has since become the production control
surface.

## Architecture

- `clipper_app.application.read_services.ReadDashboardService` reads current
  JSON and filesystem artifacts and returns typed contracts.
- `clipper_app.web_api` exposes those contracts through FastAPI under `/api`.
- `new_app/` contains the React/Vite/TypeScript frontend.
- Queue JSON, manifests, score summaries, compliance files, module indexes,
  logs, and media artifacts remain the source of truth.

Every API response uses the same envelope:

```json
{
  "data": {},
  "generated_at": "2026-06-24T13:00:00+08:00",
  "source_signatures": [],
  "warnings": []
}
```

## Original Read-Only Guarantees

Phase 2 intentionally had no queue controls, no module review mutations, no
settings persistence, no config writes, no pipeline launch controls, and no
database. Later phases added production mutation endpoints while keeping JSON
and filesystem artifacts as the source of truth.

Artifact serving is restricted to files under configured read roots:

- `OUTPUT_DIR`
- `WORKING_DIR`
- `MODULE_LIBRARY_DIR`

`pipeline.log` tailing is bounded to 1,000 lines.

## Endpoints

- `GET /api/dashboard`
- `GET /api/queue`
- `GET /api/scores`
- `GET /api/scores/{score_key}`
- `GET /api/compliance`
- `GET /api/compliance/detail?output_dir=...`
- `GET /api/modules/readiness`
- `GET /api/modules/library`
- `GET /api/logs?lines=200`
- `GET /api/settings`
- `GET /api/system`
- `GET /api/artifacts?path=...`

List endpoints support bounded pagination and filtering. Unsupported sort
fields return `400`. Artifact requests outside configured roots return `403`.
Missing explicit artifacts return `404`.

## Running Locally

Install Python dependencies first:

```powershell
python -m pip install -r requirements.txt
```

Install frontend dependencies once:

```powershell
.\run_new_app.ps1 -InstallFrontendDeps
```

Run the read API and React app side by side:

```powershell
.\run_new_app.ps1
```

Default URLs:

- React app: `http://127.0.0.1:5173`
- FastAPI: `http://127.0.0.1:8765`

## Phase 2 Deferred Work

- Start, Continue, Stop, review, rescore, compliance scan, module mutation
  actions, and persistent settings UI were moved into the production app.
- Websocket event streaming.
- SQLite or another read model store.
