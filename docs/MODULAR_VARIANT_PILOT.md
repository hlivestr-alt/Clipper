# Modular Variants Pilot (Phase B2)

This pilot adapts completed `modular-renderer-v1.1` base MP4s to the existing Variants render path. It is deliberately separate from the production queue, catalog, scoring, compliance, export, affiliate assignment, WhatsApp, and delivery.

## Existing Variants contract

Normal production does not expose a standalone post-render Variants service. Its lower reusable boundary is:

1. A base clip identity, source media path, start/end duration, canonical product, transcript words, hook metadata, and output root.
2. The active/saved variation profile, resolved by `variation_engine.expand_moments_with_variants` into the existing `v0`–`v5` recipes.
3. One expanded clip job at a time through `main._process_clip_job` and `ffmpeg_editor.render_clip`.

The pilot's generic boundary is `clipper_app.variant_generation.BaseClipArtifact`. That module has no modular database dependencies. The modular adapter validates IDs and lineage, snapshots the approved transcript/product metadata, and passes an ordinary materialized MP4 artifact to it.

## Source-accurate transcript bridge (Phase B2.1)

`modular-transcript-bridge-v1` resolves the transcript attached to each approved item's exact scanner `scan_id` and `source_id`. It also requires the scanner media identity (canonical path, size, mtime, and content fingerprint) to match the immutable planner snapshot, validates the cached transcript fingerprint, and reads the raw cache without changing Scanner. Production-origin scanner caches retain source-VOD-absolute float-second word start/end timestamps and word probabilities; Scanner's analysis loader intentionally uses only the canonical timed segments.

For each hard-cut item, the base offset is the sum of preceding approved source-range durations. A word is cropped to the source range and remapped as `base_offset + (clamped_source_time - item_start)`. Fully contained words are retained. A boundary-straddling word is retained when at least 40 ms overlaps or its midpoint is inside the half-open range; after clamping, intervals shorter than 20 ms are dropped. Every result is validated for positive, monotonic intervals and confinement to its own item/base/rendered duration.

The fallback order is explicit per item: source word timestamps, deterministic token distribution within real source transcript segments, then the legacy approved-manifest distribution. The run request identity includes the bridge version, and item diagnostics persist transcript identities, modes, crop counts, fallback reasons, timing examples, validation, and resolution/crop/validation duration. Existing B2 records retain the default `legacy-manifest-synthetic` marker and are not rewritten.

## Persistence and output

- State: `working/modular_variant_pilot.sqlite3`
- Output: `working/modular_variant_pilot/<run_id>/<ordinal>_<composition_id>_<product>/`
- Report: `pilot_report.json` in the run directory
- One durable worker processes one base at a time.
- Restart recovery requeues only incomplete items. Existing valid variant files are reused through the normal render validation path.
- The request key covers selected render IDs, base-file identities, and profile revision. Repeated requests reuse active/completed runs unless `manual_rerun` is explicit.

## Eligibility and provenance

The public API accepts render/composition IDs only. The adapter requires a completed, present base MP4; renderer version 1.1 or newer; renderer diagnostics; an approved immutable planner manifest; and matching canonical product metadata. Pilot state retains render run/item, planner run, composition, product, renderer version, and SHA-256 base identity.

## API

- `GET /api/modular-variant-pilot/profiles`
- `GET /api/modular-variant-pilot/eligible?planner_run_id=...`
- `POST /api/modular-variant-pilot/runs` (202)
- `GET /api/modular-variant-pilot/runs`
- `GET /api/modular-variant-pilot/runs/{run_id}`
- `GET|HEAD /api/modular-variant-pilot/media/{media_id}`

All endpoints use the existing authenticated control boundary. Media IDs are opaque; paths are never accepted or returned.

## Safety boundary

The pilot uses the existing profile expansion, crop/zoom, B-roll, subtitle, overlay, text, audio, transition, and naming behavior. No modular-specific recipes exist. `COMPLIANCE_ENABLED` is disabled only in the pilot runtime wrapper, and no scoring/export/delivery orchestration is called. `video_queue.PIPELINE_MODE_STAGES` and normal launcher behavior are unchanged.

The generic renderer's `RENDER_REQUIRE_TARGET_SIZE` default remains `True`. The pilot sets it to `False` because these review artifacts are not delivery payloads and ~60-second variants can legitimately exceed the WhatsApp byte cap. Decode, stream, duration, geometry, and atomic finalization checks still run.
