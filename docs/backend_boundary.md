# Backend Application Boundary

`clipper_app` is the stable application layer for production operations. It
wraps the existing pipeline algorithms without changing JSON schemas, artifact
paths, cache fingerprints, CLI flags, or scheduling behavior.

## Package Structure

- `contracts`: strict, frozen Pydantic command, result, settings, and event models.
- `application`: pipeline, queue, scoring, compliance, module, and health services.
- `adapters`: compatibility adapters for existing filesystem and Python modules.
- `bootstrap.py`: composition functions used by CLI and FastAPI entry points.

New callers should use services from `clipper_app.bootstrap`. Direct algorithm
functions remain available for compatibility and maintenance utilities.

## Compatibility Mode

The service boundary is enabled by default. Set the environment variable below
to bypass the pipeline and queue service facades during the production soak:

```powershell
$env:CLIPPER_SERVICE_BOUNDARY = "legacy"
```

Unset it or set it to `service` to use the application boundary. Both modes run
the same orchestration implementation and write the same artifacts. The switch
changes only command validation, settings composition, and progress-event
adaptation.

## Settings

`LegacyConfigProvider` reads the final evaluated values from `config.py` and
creates an immutable `SettingsSnapshot`. Runtime command overrides take
precedence over snapshot values, which take precedence over unregistered legacy
configuration values.

The registry contains operator-safe scalar settings in these categories:

- Paths and queue scheduling
- LM Studio, Whisper, and model controls
- Clip selection thresholds
- Common render, audio, variants, and silence trimming controls
- Scoring and compliance controls
- Module extraction, assembly, validation, and readiness controls

Correction dictionaries, YOLO class maps, training settings, secrets, fonts,
layout geometry, and specialized asset mappings remain legacy-only. Phase 1
does not write settings or modify `config.py`.

## Events

Pipeline progress is normalized into `ProgressEvent` with a stable operation ID,
timestamp, stage, event kind, percentage, message, counters, clip identity, and
artifact references. Event sinks support in-memory tests, structured logging,
legacy callbacks, and queue-state projection.

The current event kinds are:

- `progress`
- `clip_batch_start`, `clip_started`, `clip_complete`
- `clip_scoring_progress`, `render_paused`
- `module_extraction_complete`, `modular_clip_complete`
- `module_assembly_complete`, `pipeline_complete`

## Preserved Contracts

- Queue state schema version `2`
- Queue control schema version `1`
- Supervisor state and paused exit code `10`
- `PipelinePaused`, retries, resume behavior, and stage fingerprints
- `pipeline.log`, manifests, score/compliance sidecars, and output naming
- Existing CLI and PowerShell launcher arguments

SQLite, HTTP APIs, persistent settings, and the replacement frontend are
deliberately deferred to later phases.
