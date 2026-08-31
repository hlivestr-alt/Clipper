# Modular Renderer v1.1

`clipper_app/modular_renderer/` is the Phase B1 pilot materializer. It is deliberately separate from the Modular Scanner, Modular Planner, and the normal production queue.

## Boundary

The renderer accepts only composition IDs from an immutable approved planner manifest. It reads the manifest's persisted source path, source metadata, scanner content fingerprint, item order, and exact `start_seconds` / `end_seconds`. It never queries current scanner segments or changes planner state.

The renderer does not invoke Variants, scoring, compliance, export, delivery, or normal base-clip registration.

## Storage

Execution state is stored in `working/modular_renderer.sqlite3`:

- `modular_render_runs` records the planner run/manifest, renderer version, selection, output directory, lifecycle state, counts, and current composition.
- `modular_render_items` records composition/product/template/ordinal, expected and rendered durations, delta, output path, lifecycle/error state, timing metrics, normalization metadata, and additive per-segment diagnostics JSON. Schema v1 databases migrate in place; existing v1 rows read with empty diagnostics.

Outputs are isolated under:

```text
working/modular_renders/<render_run_id>/
  001_<composition_id>_<product>.mp4
  render_report.json
```

Successful item intermediates are removed. Failed item intermediates remain under the run's `temp/` directory for diagnosis.

## Media behavior

Each distinct source is checked once per render run where practical. Verification enforces the configured VOD root, existence, exact size and mtime snapshot, and the scanner's existing fast content-fingerprint algorithm.

Items are decoded from the exact approved range and normalized to the first item's even-numbered geometry, 30 fps CFR H.264 (`libx264`, preset `fast`, CRF 20, GOP 60, `yuv420p`) and 48 kHz stereo AAC at 192 kbps. Matching source geometry is preserved. Differing geometry is aspect-preserving scaled and padded only as needed for reliable concatenation. Missing source audio fails that composition because synthetic silence is outside the pilot scope.

The v1.1 fast path performs a whole-second input seek to approximately five seconds before the approved start, then applies an accurate output-side local trim and the exact approved duration. The whole-second anchor preserves the v1 30 fps and 48 kHz filter phase while bounding pre-range decoding to the local GOP/preroll instead of timestamp zero. The intermediate is probed before use. Missing streams, FFmpeg errors, duration mismatch over 0.15 seconds, or audio/video duration divergence reject and delete the fast output, then deterministically retry the original v1 output-seek command. Stream copy is never used for extraction.

Per-segment diagnostics record composition/position, public source identity, requested start/end/duration, source and intermediate durations, extraction and validation timing, coarse/local seek values, geometry-normalization need, accepted strategy, and fallback outcome. Canonical source paths are not included. Source fingerprints and source probes are cached independently for the lifetime of one render run without weakening fingerprint checks.

Normalized intermediates are concatenated in manifest position order with the FFmpeg concat demuxer and plain cuts. There are no transitions, padding, silence detection, speech trimming, volume/loudness processing, music, or creative edits.

The final MP4 is written to a `.partial.mp4`, probed for video/audio/container/duration validity, and atomically renamed. Duration tolerance is at least 0.5 seconds and grows only by frame-rounding allowance for the number of joined items.

## Execution and recovery

`POST /api/modular-renderer/runs` persists work and returns HTTP 202. One daemon worker renders compositions sequentially. Before each new composition it reads the same production-busy snapshot used by the scanner; it records `waiting_for_production` and resumes automatically when production is idle. An already-running FFmpeg operation is allowed to finish before the next busy check.

On restart, interrupted `rendering` or `waiting_for_production` items return to `queued`; completed items remain completed. A valid atomically finalized output left between rename and database commit is probed and adopted rather than rendered again. A partial file is never marked completed.

Equivalent active launches are reused. A valid completed launch is exposed by default; an explicit `manual_rerender` creates a new run and never overwrites the prior output.

## API

- `POST /api/modular-renderer/runs`
- `GET /api/modular-renderer/runs?planner_run_id=...`
- `GET /api/modular-renderer/runs/<render_run_id>`
- `GET|HEAD /api/modular-renderer/runs/<render_run_id>/media/<composition_id>`

All routes use the existing control authentication boundary. Playback resolves media only through renderer-owned IDs and supports byte ranges; filesystem paths are not accepted or returned.
