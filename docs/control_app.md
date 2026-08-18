# Control App

The FastAPI + React application is Clipper's main production operations, review, and configuration surface. It reads the same artifacts used by the CLI and submits controlled backend work; it does not reimplement video processing in the browser.

## Operator Surfaces

The current routes are:

- `/overview`: production, quality, compliance, and export health.
- `/production/live` and `/production/queue`: run launcher, active progress, queue rows, and graceful controls.
- `/review/clips` and `/review/compliance`: paginated score/variant review, artifact previews, rescore, violations, and compliance scans.
- `/trends`: TikTok Business OAuth/advertiser state, ranked discovery, rights-confirmed downloads, media linking, analysis, and read-only editing recommendations.
- `/variants`: revision-safe profile editing, seven grouped editor tabs, previews, presets, product information, and asset diagnostics.
- `/modules`: neutral placeholder while the modular workspace is rebuilt separately.
- `/deliveries`: export-batch status, preflight, and recovery packaging.
- `/activity/jobs` and `/activity/logs`: job ledger/results and bounded log tailing.
- `/settings/configuration` and `/settings/diagnostics`: safe settings overrides, system/catalog/SSE status, and Electron runtime diagnostics.

Legacy route names redirect to these current routes.

## Queue Launch Model

New VOD work still enters through the queue. The UI supports:

- `single_video`, `folder_once`, or `folder_repeat` run modes.
- `full`, `clips_only`, or `raw_cuts_only` pipeline modes. Historical `modules_only` records display as legacy unsupported and cannot be resumed.
- all variants, original only, or a custom count from 1 through 6.
- an optional maximum clip count.

Start, pause, continue, and stop requests use the existing versioned queue-control files and supervisor behavior. They do not mutate a running pipeline's settings in memory.

## Mutation and Job Model

Queue controls, settings writes/deletes, rescore, compliance scan, export packaging, and trend refresh/download/analysis become auditable `ControlJob` records. Those endpoints return HTTP `202`, and the UI follows job state while SSE/query invalidation refreshes affected reads.

Variation/profile/preset writes, preview generation, product-information rescan, TikTok advertiser selection, trend media linking, and WhatsApp delivery transitions are synchronous service mutations. They return their result directly and use revision, validation, idempotency, containment, or domain-specific audit/event rules instead of `ControlJob` metadata.

Storage is split so large operation results do not bloat job metadata:

- `working/app_control_jobs/`: schema-version-2 metadata.
- `working/app_control_job_results/`: bounded downloadable JSON results.
- `working/app_control_audit.jsonl`: append-only transitions and rejection details.

Default retention is 30 days/2,000 terminal metadata records and seven days/250 MiB of result files. Result bodies are capped at 5 MiB and can be marked truncated or expired. Startup converts stale queued/running jobs to `interrupted`; `CLIPPER_MIGRATE_JOB_STORAGE=1` migrates older embedded results.

Interactive and batch work use fixed daemon-worker lanes with bounded pending capacity. Compute-heavy work is serialized. Conflicting rescore/compliance targets, global export packaging, duplicate trend work, and queue controls are rejected with conflict information.

## Settings and Variations

The app writes safe overrides to `working/settings_overrides.json`; it never writes `config.py`. Values are type/range/relationship validated, privileged keys are read-only, and revision checks prevent stale-page overwrites. Precedence is runtime command override, persisted app override, then `config.py`.

Variation profiles are separate from settings:

- Active profile: `working/variation_profile.json`.
- Presets: `working/variation_presets/*.json`.
- Generated previews: `working/variation_previews/`.

Applying a profile affects future clip generation only. It does not rewrite already-rendered outputs. See [../VARIANT_PAGE_AUDIT.md](../VARIANT_PAGE_AUDIT.md).

## API Groups

Major read groups include health/catalog/events, dashboard/overview/queue/VODs, scores, compliance, logs, settings, system, artifacts, product information, variations, trends, TikTok OAuth status, control jobs, and WhatsApp delivery status/outbox.

Major mutation groups include queue control, settings override writes/deletes, product-information rescan, variation save/preview/presets, rescore, compliance scan, export packaging, trend refresh/download/analysis/media link, TikTok OAuth/advertiser selection, and WhatsApp claim/assignment/item/outbox transitions.

The route definitions in `clipper_app/web_api.py` are the authoritative endpoint list.

## Security Boundary

Every mutation and sensitive read requires `Authorization: Bearer <CLIPPER_CONTROL_TOKEN>`. Artifact, catalog, log, effective-setting, job-result, trend, TikTok integration, and WhatsApp reads are sensitive. TikTok callback routes are deliberately unauthenticated and strip the authorization code from the normal query string before downstream handling.

Electron injects a fresh per-launch token only into requests for its exact managed `127.0.0.1:<port>` origin. The development launcher shares a token with the Vite proxy. Trusted-host, origin, and path-containment checks remain active in both cases.

## Running Locally

```powershell
python -m pip install -r requirements.txt
.\run_new_app.ps1 -PnpmExe pnpm.cmd -InstallFrontendDeps
.\run_new_app.ps1 -PnpmExe pnpm.cmd
```

Defaults are React at `http://127.0.0.1:5173` and FastAPI at `http://127.0.0.1:8765`.

For a built same-origin SPA:

```powershell
pnpm.cmd --dir new_app build
$env:CLIPPER_CONTROL_TOKEN = '<strong temporary value>'
$env:CLIPPER_CONTROL_ACTOR = 'local:operator'
python -m uvicorn clipper_app.web_api:app --host 127.0.0.1 --port 8000
```

The built browser client itself does not expose or persist that token. Use Electron for the complete local control experience, or keep a browser deployment read-limited until an approved authentication/token-forwarding layer exists.

## Current Boundaries

- No direct social-platform publishing is implemented.
- TikTok recommendations are displayed but not automatically applied to profiles.
- JSON/filesystem reads and queue state remain the default authority until staged SQLite cutover.
- WhatsApp assignment/send APIs exist, but direct claims remain disabled until both cutover flags are explicitly enabled.
- The app requires the external Python/FFmpeg/LM Studio/CUDA/models/data environment; video work does not run inside React or Electron.
