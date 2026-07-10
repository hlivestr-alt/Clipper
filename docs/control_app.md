# Control App

Phase 3 moves daily production controls into the FastAPI + React app while
preserving the existing JSON/filesystem source of truth.

The FastAPI + React app is the production control surface for normal
operations. New VOD work still enters through the queue workflow.

## Mutation Model

All app mutations go through `clipper_app.application.control_services`:

- Jobs are stored under `working/app_control_jobs/`.
- Audit entries append to `working/app_control_audit.jsonl`.
- Settings overrides are stored in `working/settings_overrides.json`.
- `config.py` is never written by the app.

Every mutation endpoint returns the standard API envelope with a `ControlJob`
in `data` and HTTP `202`. The frontend polls `/api/control/jobs` to show job
status and errors.

Job statuses are:

- `queued`
- `running`
- `completed`
- `failed`
- `interrupted`
- `rejected`

On API startup, stale queued or running job files from a previous process are
marked `interrupted`.

## Settings

Only keys in the Phase 1 safe settings registry can be written. Values are
validated for type and bounds before persistence.

Precedence is:

1. Command/runtime override
2. Persisted app override in `working/settings_overrides.json`
3. Legacy value from `config.py`

The app uses revision checks when saving settings so stale pages cannot silently
overwrite newer values. Overrides apply to future service snapshots and future
queue/supervisor runs; running pipeline processes are not mutated.

## Mutation Endpoints

- `POST /api/control/queue`
- `GET /api/control/jobs`
- `GET /api/control/jobs/{job_id}`
- `GET /api/settings/effective`
- `PUT /api/settings/overrides`
- `DELETE /api/settings/overrides/{name}`
- `POST /api/operations/rescore`
- `POST /api/operations/compliance-scan`
- `POST /api/operations/module-assembly`
- `POST /api/operations/export-batches`
- `POST /api/modules/{module_id}/review`

Filesystem-targeting operations validate their target paths before accepting a
job. Rescore and compliance scan targets must resolve under `OUTPUT_DIR`.
Export packaging targets must resolve under `OUTPUT_DIR`. Module review accepts
indexed module identifiers, not arbitrary paths.

## Concurrency Rules

The job layer rejects conflicting active jobs:

- Rescore conflicts by `output_dir`.
- Compliance scan conflicts by `output_dir`.
- Module assembly conflicts globally.
- Export packaging conflicts globally.

Fast jobs such as queue control and settings writes still create job and audit
records.

## Frontend Surfaces

The React app includes:

- Persistent Control Center with recent jobs and errors.
- Queue Start, Continue, Graceful Stop, and Status controls.
- Editable Settings page for registry-backed values.
- Rescore action panel on Scores.
- Compliance scan action panel on Compliance.
- Module assembly and module review panels on Modules.
- Export packaging page.
- Existing read views for queue, scores, compliance, modules, logs, settings,
  system health, and artifacts.

Risky actions use inline confirmation controls.

## Deferred

- Direct new-VOD launch outside the queue workflow.
- Electron wrapper.
- Database-backed job/read models.
- Websocket event streaming.
- Broad visual polish pass.
