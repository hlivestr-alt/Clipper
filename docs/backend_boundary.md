# Backend Application Boundary

`clipper_app` is the stable application layer around the existing top-level pipeline modules. It gives CLI, queue, FastAPI, and future automation callers typed commands, results, settings, events, reads, and controlled mutations without changing the legacy algorithms or their artifact contracts.

## Package Structure

- `clipper_app/contracts/`: strict Pydantic pipeline, queue, read, control, event, variation, trend, and WhatsApp delivery models.
- `clipper_app/application/services.py`: pipeline, queue, scoring, compliance, module, export, and health facades.
- `clipper_app/application/read_services.py`: legacy-filesystem and optional catalog-backed dashboard reads.
- `clipper_app/application/control_services.py`: safe settings persistence and bounded background jobs.
- `clipper_app/application/catalog.py`: rebuildable SQLite read model, change events, and trend records.
- `clipper_app/application/queue_repository.py`: JSON, dual-write, and SQLite queue persistence.
- `clipper_app/application/tiktok_oauth.py` and `trends.py`: TikTok Business authorization, discovery, media, and editing-pattern analysis.
- `clipper_app/application/whatsapp_delivery.py`: canonical WhatsApp batch, assignment, item, audit, and Sheet-outbox state.
- `clipper_app/application/api_security.py`: Bearer-token, trusted-host, origin, and sensitive-read rules.
- `clipper_app/application/container.py`: FastAPI service composition.
- `clipper_app/adapters/legacy.py`: adapters that call the existing top-level implementations.
- `clipper_app/bootstrap.py`: composition helpers used by CLI entry points.
- `clipper_app/web_api.py`: FastAPI routes, middleware, static SPA serving, and SSE invalidation.

New Python callers should prefer the builders in `clipper_app.bootstrap` or the shared container. Top-level algorithm functions remain available for compatibility and maintenance tools.

## Compatibility Modes

The typed service boundary is the default for `main.py`, queue, supervisor, and control entry points. To bypass only that facade during a compatibility investigation:

```powershell
$env:CLIPPER_SERVICE_BOUNDARY = "legacy"
```

Unset it or use `service` for normal operation. Both paths use the same pipeline implementation and preserve CLI flags and artifacts.

Storage migration is controlled separately:

- `CLIPPER_CATALOG_MODE=legacy` (default): reads come from filesystem artifacts.
- `CLIPPER_CATALOG_MODE=shadow`: legacy reads remain authoritative while the catalog is indexed and compared.
- `CLIPPER_CATALOG_MODE=catalog`: supported score, compliance, module, overview, and output reads use SQLite.
- `CLIPPER_QUEUE_STORAGE_MODE=json` (default): legacy queue JSON is authoritative.
- `CLIPPER_QUEUE_STORAGE_MODE=dual`: JSON reads plus SQLite/history writes.
- `CLIPPER_QUEUE_STORAGE_MODE=sqlite`: SQLite is authoritative and an active-only compatibility JSON snapshot is refreshed.

See [long_term_storage_rollout.md](long_term_storage_rollout.md) before changing a storage mode.

## Settings

`LegacyConfigProvider` evaluates `config.py`, overlays `working/settings_overrides.json`, and creates an immutable `SettingsSnapshot`. Precedence is:

1. Operation/command overrides.
2. Persisted settings overrides.
3. Evaluated values from `config.py`.

Only names in `SETTINGS_REGISTRY` are persisted. Browser writes are further restricted to `BROWSER_EDITABLE_SETTINGS`, validated for type, bounds, and cross-setting relationships, and protected by revision checks. Privileged paths, credentials, class maps, dictionaries, and similar machine/operator-managed values remain read-only. Neither the web app nor the service layer writes `config.py`.

## FastAPI and Security

Every JSON read uses a common envelope with `data`, `generated_at`, `source_signatures`, and `warnings`. Read responses can also use source revisions/ETags.

All non-safe HTTP methods and sensitive reads require `Authorization: Bearer <CLIPPER_CONTROL_TOKEN>`. If no token is configured, those routes return `503`; an invalid or missing token returns `401`. The two TikTok callback paths are the deliberate exception. Trusted hosts and CORS/same-origin rules come from `CLIPPER_ALLOWED_HOSTS` and `CLIPPER_ALLOWED_ORIGINS` plus loopback defaults.

Electron generates a fresh token and injects it only for the managed loopback origin. `run_new_app.ps1` does the same through the Vite development proxy. A compiled browser-only deployment does not currently have a user-facing token mechanism; see [cloudflare_dashboard_access.md](cloudflare_dashboard_access.md).

Filesystem endpoints preserve containment checks under configured output, working, module-library, and approved trend-media roots. API requests cannot select arbitrary queue-state/log paths or arbitrary module paths.

## Jobs and Audit

Mutations are represented by schema-version-2 `ControlJob` records:

- Metadata: `working/app_control_jobs/<job-id>.json`.
- Bounded result bodies: `working/app_control_job_results/<job-id>.json`.
- Append-only audit: `working/app_control_audit.jsonl`.

Result files are capped at 5 MiB, retained for seven days by default, and constrained to 250 MiB total. Terminal metadata is retained for 30 days with a 2,000-record cap. Legacy embedded job results can be migrated when `CLIPPER_MIGRATE_JOB_STORAGE=1`; Electron and `run_new_app.ps1` set that flag.

The scheduler has one interactive worker and two batch workers with bounded pending queues. Compute-heavy batch jobs are serialized, while non-compute export work can occupy the second batch worker. Conflict keys reject overlapping work on the same logical target. Startup marks stale queued/running metadata as `interrupted`.

Statuses are `queued`, `running`, `completed`, `failed`, `interrupted`, and `rejected`.

## Events and Cache Invalidation

Pipeline progress is normalized into `ProgressEvent` while preserving legacy callback payloads. Change events are stored in SQLite and exposed through `/api/events` as durable server-sent events with IDs, replay, retention-gap reset notices, and 15-second heartbeats. The React app invalidates affected query prefixes and falls back to polling when SSE is unavailable. Set `CLIPPER_PUSH_INVALIDATION=0` to disable the endpoint and force polling.

## Preserved Contracts

- Existing `main.py`, queue, supervisor, control CLI, and PowerShell arguments.
- Queue control schema version 1 and supervisor pause/stop exit behavior.
- Legacy queue schema version 2 for rollback export; active SQLite compatibility snapshots use queue schema version 3.
- `PipelinePaused`, retries, resume state, stage fingerprints, and render manifests.
- `pipeline.log`, score/compliance/module/export sidecars, output naming, and filesystem authority in default modes.
- Legacy JSON reads and immediate rollback while the catalog and queue migrations remain staged.

SQLite, HTTP mutations, persistent settings, SSE, TikTok research, WhatsApp delivery state, and Electron are implemented now; they are no longer deferred boundary work.
