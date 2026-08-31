# Modular Video Production

## Production methods

Clipper has two source methods for finished clips:

- `standard` starts from the existing VOD clip-detection pipeline. Its pipeline stages remain `full`, `clips_only`, and `raw_cuts_only`.
- `modular_video` starts from approved compositions assembled from scanner segments.

`modular_video` is not a `video_queue.PIPELINE_MODE_STAGES` value. The historical `modules_only` value remains unsupported.

## Production flow

The production path is:

1. `modular-planner-v1.1` creates composition drafts.
2. The ordinary planner approval operation freezes the immutable approved manifest.
3. `modular-renderer-v1.1` renders base videos from exact source ranges.
4. `modular-transcript-bridge-v1` maps source word timestamps onto each base timeline.
5. The selected existing Variants profile expands each ordinary `BaseClipArtifact`.
6. The existing compliance service scans the finished Variants manifest.
7. The existing scoring service scores accepted finished clips and places them in the normal output tiers.
8. The finished run remains in the normal output structure. Affiliate batch intake and WhatsApp delivery remain separate and are not triggered by Modular Production.

No scanner, planner, renderer, subtitle, Variants, compliance, or scoring quality rules are reimplemented in the orchestrator.

## Operator workflows

### Automatic

Automatic is the default. Creating a job returns HTTP 202 and background orchestration creates a normal planner run, approves its active drafts using the normal invariant checks, records the immutable planner manifest ID, and continues through all stages.

A planner shortfall is preserved. If at least one valid composition exists, the job continues using the generated count and records requested, generated, and shortfall values.

### Review First

Review First creates a normal planner run and stops in `awaiting_review`. The existing planner review UI remains authoritative for preview, regeneration, and removal. `Continue Production` approves the current planner revision with the normal approval operation if it is still a draft, records the immutable manifest, and starts rendering. Rendering cannot start before this transition.

## Persistence and recovery

Orchestration state is stored additively in `working/modular_production.sqlite3`. Existing scanner, planner, renderer, and pilot databases are not migrated or rewritten.

The database contains:

- `modular_production_jobs`: frozen request/profile data, subsystem references, stage, counters, warnings, timing, cancellation, and timestamps.
- `modular_production_items`: per-composition render and Variants state plus transcript bridge diagnostics.
- `modular_production_variants`: per-Variant durable output identity and lineage.

On startup, non-terminal jobs are queued from their durable stage. The orchestrator checks referenced planner and renderer state, preserves completed base and Variant rows, validates/reuses already published Variant files through the existing renderer behavior, and resumes the earliest incomplete downstream stage. It does not create a second planner run when `planner_run_id` is already stored.

## Idempotency and reruns

An active-job request key includes product, workflow, planner settings, count, seed, and the frozen Variants profile revision. Repeating Start with the same inputs returns the active job. `explicit_rerun=true` creates a new job linked through `rerun_of_job_id`; prior output is never overwritten.

The selected Variants profile JSON and revision are frozen when the job is created. Later profile edits do not change an active job.

## Partial completion and cancellation

Base and Variant work is accounted independently. A failed base does not prevent successful bases from continuing. A Variant failure preserves already published Variant rows and allows other bases to proceed. Compliance rejection, scoring count, and final exported count remain distinct.

Terminal states are:

- `completed`: every generated finished Variant reached normal final output.
- `completed_with_failures`: useful output exists with base, Variant, compliance, scoring, or export loss.
- `failed`: no useful output survived or a global invariant failed.
- `cancelled`: the operator requested cooperative stop.

`Stop after current operation` sets a durable cancellation request. The orchestrator stops starting new work after the active internal operation returns; it does not kill FFmpeg during atomic publication. Completed artifacts remain available.

## Production priority

Renderer and Variants admission continue using the existing Standard Production busy detector. Modular Production state is stored separately, so it does not identify its own orchestration job as a Standard queue run and cannot deadlock on itself. Standard Production can retain its existing priority without a new resource coordinator.

## Lineage

Each final Variant records IDs and compact provenance:

- Modular Production job, product, planner run, planner manifest, composition, render run, and render item.
- Variants profile ID/revision, Variant index/ID/name.
- scanner, planner, renderer, and transcript bridge versions.
- source fingerprint chain.
- ordered segment, scan, source VOD ID, role, and source timestamps.
- transcript bridge diagnostics, including source-word timing provenance.

Canonical VOD paths and output paths are internal only. Authenticated media endpoints expose artifacts by media ID.

## API

Authenticated production endpoints are:

- `GET /api/modular-production/profiles`
- `POST /api/modular-production/jobs`
- `GET /api/modular-production/jobs`
- `GET /api/modular-production/jobs/{job_id}`
- `POST /api/modular-production/jobs/{job_id}/continue`
- `POST /api/modular-production/jobs/{job_id}/cancel`
- `GET|HEAD /api/modular-production/media/{media_id}`

## Intentionally unchanged/out of scope

Scanner remains `modscan-v3.2` with `modscan-prompt-v3`; Planner remains `modular-planner-v1.1`; Renderer remains `modular-renderer-v1.1`; and the transcript bridge remains `modular-transcript-bridge-v1`. No ASR correction, rescanning, planner reinterpretation, renderer quality change, modular-specific Variant recipe, compliance rule, scoring rule, affiliate delivery, or WhatsApp behavior is added.

Historical Modular Renderer and B2/B2.1 Variants pilot records and playback endpoints remain readable. `D:\proya_modules` is not used or modified.
