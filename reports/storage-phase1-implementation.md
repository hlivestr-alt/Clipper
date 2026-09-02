# Clipper Storage Phase 1 Implementation

Implementation date: 2026-09-01 (Asia/Shanghai)
Starting point: `reports\storage-audit.md`

## Summary

Phase 1 is implemented. New tagged runs no longer copy full transcripts or raw transcription checkpoints into each run folder. New managed media has explicit ownership and lifecycle metadata; clip moves are journaled through a shared publisher; newly created raw cuts are retired only after a validated successor and committed manifest exist; and the 119 stale modular-production output paths identified by the audit were deterministically reconciled.

The storage inventory and reclamation planner are explicit, metadata-only, cached operations. The planner has no deletion implementation in Phase 1 and treats all legacy or unknown ownership as `KEEP`.

**Historical bulk cleanup was NOT performed.** No VOD, legacy run, existing variant, output corpus, Git history, or historical working folder was deleted or deduplicated.

## Files Changed

Storage architecture:

- `clipper_app\storage\models.py` — lifecycle and cleanup classifications.
- `clipper_app\storage\registry.py` — SQLite artifact, reference, publish, raw, reconciliation, and inventory records.
- `clipper_app\storage\transcripts.py` — canonical transcript identity, commit validation, locking, references, and legacy resolution.
- `clipper_app\storage\publishing.py` — verified, journaled clip moves and publish transitions.
- `clipper_app\storage\raw_lifecycle.py` — new-operation raw ownership and conservative retirement rules.
- `clipper_app\storage\inventory.py` — on-demand inventory and zero-deletion reclamation planning.
- `clipper_app\storage\reconciliation.py` — identity-backed modular-production path repair.
- `clipper_app\storage\asset_roles.py` — content IDs with multiple logical B-roll roles.
- `clipper_app\storage\__init__.py` and `storage_cli.py` — package and operator CLI.

Application integration:

- `transcriber.py`, `video_queue.py`, `stage_cache.py`, `compliance_checker.py`, and `main.py` now resolve and serialize canonical transcript references.
- `clipper_app\modular_scanner\service.py` and `clipper_app\modular_scanner\transcripts.py` reuse canonical/production transcript paths without creating a scanner copy.
- `main.py` records new raw cuts and new pending outputs, and only retires a raw after durable success evidence.
- `clip_scorer.py` and `export_packager.py` publish managed moves with lifecycle and operation evidence.
- `clipper_app\modular_production\service.py` registers new variants as pinned `PENDING` artifacts and records dependency-aware render lineage.
- `config.py` adds review and rejected retention settings. `.gitignore` blocks new runtime, cache, log, dependency, and build output additions.
- `test_storage_phase1.py`, `test_video_queue.py`, and `test_modular_scanner_execution.py` cover the new behavior.
- `reports\storage-reconciliation.md` contains the applied path reconciliation evidence.

## Canonical Transcript Architecture

Canonical transcript artifacts live at:

`working\artifacts\transcripts\<artifact-id>\`

Each committed artifact can contain one `transcript.json`, one reusable `transcript.raw_checkpoint.json`, and an `artifact.json` manifest. A tagged run contains only the small `transcript.artifact.json` reference.

The deterministic artifact ID covers:

- source byte identity, size, and modification identity;
- full SHA-256 for inputs up to 64 MiB, or a versioned first/middle/end 1 MiB sampled SHA-256 plus size/mtime for larger VODs;
- transcriber implementation and installed package version;
- model, language, compute, beam, best-of, corrections, VAD, and word-timestamp settings;
- alignment implementation, installed package version, backend, model, interpolation, device, subprocess/fallback, and segment settings;
- transcript schema and artifact schema revisions.

Writes use per-artifact locks and uniquely named `.partial` staging directories. A directory becomes reusable only after atomic publication of a `COMMITTED` manifest containing size and SHA-256 evidence for every payload. Missing, interrupted, corrupt, or fingerprint-incompatible artifacts do not resolve.

The original duplication came from `_reuse_base_transcript_for_tagged_run` explicitly calling `shutil.copy2` for both `transcript.json` and `transcript.raw_checkpoint.json` into each new tagged working folder. It now finds an existing canonical artifact or imports one compatible legacy transcript/checkpoint exactly once, then attaches a small reference. New tagged runs and the modular scanner do not create physical transcript/checkpoint duplicates.

## Artifact Lifecycle Model

The registry is `working\artifacts\artifact_registry.sqlite3`. It stores artifact identity, type, canonical path, size, content/fingerprint identity, owner, lifecycle, state, regeneration evidence, pin state/reason, timestamps, active references, publish operations, raw intermediates, reconciliation events, and indexed inventory metadata.

Implemented lifecycle classes are:

- `PERMANENT_STATE`
- `SOURCE`
- `FINAL`
- `EXPORT`
- `PENDING`
- `REGENERABLE`
- `CACHE`
- `TEMP`
- `REVIEW`
- `REJECTED`
- `UNKNOWN`

`PERMANENT_STATE`, `SOURCE`, `FINAL`, `EXPORT`, and `PENDING` are pinned by default. Active references block cleanup. `REGENERABLE` requires positive, available dependency evidence; a Boolean label alone is insufficient. Missing registry ownership remains `UNKNOWN` and is protected.

Managed publication records `PREPARED`, moved, verified, reconciled, and `COMMITTED` evidence. Cross-volume publication uses a verified partial copy and atomic target replacement before the source is retired. Existing scorer and export moves now use this service; atomic export-batch directory transitions are registered after commit.

## Raw Lifecycle Changes

Every newly created raw cut is registered with its owning clip job and stage. Failed or interrupted operations remain `FAILED_RETAINED` or `INTERRUPTED_RETAINED` with retry required.

A new raw is removed only when all enforced evidence is present:

- its successor is a non-empty file;
- delivery validation is compliant;
- the owning clip has a successful, committed manifest row;
- the raw belongs to the current managed operation;
- its retry requirement has been cleared as part of the durable transition.

Missing validation, missing successor, missing/failed manifest state, or any lifecycle exception retains the raw. Historical/untracked raws are never removed. The terminal-leftover collector is dry-run only and rejects execution mode.

## Output Path Reconciliation

The modular-production database contained 216 variant rows, of which 119 paths were stale. Reconciliation used job/manifest `media_id` and `clip_id`, exact expected filenames inside controlled lifecycle tiers, plus recorded publish history where available. It never accepted a basename-only match.

Result:

- stale before: 119;
- strongly resolved and updated: 119;
- ambiguous: 0;
- missing/manual: 0;
- stale after: 0;
- media deleted: 0.

Every update preserved the previous and resolved paths, classification, reason, identity evidence, timestamp, and applied status in the registry. The operation is idempotent. Full row-level evidence is in `reports\storage-reconciliation.md`.

## Dry-Run Storage Planner

`storage_cli.py inventory`, `plan`, and `build-inventory` invoke an explicit service; none runs during normal startup. Scans use filesystem metadata and an indexed SQLite cache rather than recursively hashing the multi-terabyte media roots.

The latest full project plan examined 134,666 files. Because this is a pre-migration corpus, all 134,666 were classified `BLOCKED_UNKNOWN_OWNER`; there were zero safe candidates and the current strictly proven reclaimable amount is **0 B**. This is deliberately more conservative than the audit's estimated **13.8–15.2 GiB** of eventual policy-based project reclamation.

The service also completed a full metadata scan of `D:\VOD` (937,249,398,486 bytes, about 872.97 GiB) and a bounded 1,000-file `D:\output_clips` smoke scan. A full 2.10 TiB output scan was not needed to prove the architecture and was intentionally not repeated during implementation. The planner output is `reports\storage-reclamation-plan.json`; it lists positive candidates in full, classification counts, and bounded blocked examples. Its `execute` method always raises `PermissionError` in Phase 1.

The development-build inventory found 616,073,916 bytes across `new_app\dist` and `new_app\dist-desktop`. It is reporting only; nothing was removed.

## output_clips Retention Readiness

New generated/modular variants enter as pinned `PENDING`. Scoring publishes them as `EXPORT`, `REVIEW`, or `REJECTED`; export packaging preserves `EXPORT` or `PENDING` as appropriate. `FINAL`, `EXPORT`, and `PENDING` cannot be reclamation candidates. Review and rejected retention defaults are configurable at 30 and 14 days, but age alone never makes an artifact safe.

Modular production lineage now records source/fingerprint lineage, planner manifest and composition identity, rendering/variant metadata, transcript dependencies, product, and selected output identity. New renders are marked `METADATA_COMPLETE_DEPENDENCIES_UNVERIFIED`, not falsely `REGENERABLE`, until every required source and dependency is verified as available.

The B-roll role model computes a physical asset content ID and supports multiple logical role/product/order/name records without changing visible names, deterministic ordering, associations, or selection behavior. Existing duplicate trees were not migrated.

## Compatibility / Legacy Behavior

Legacy run-local `transcript.json` remains valid and is preferred when present. Otherwise, callers resolve a validated `transcript.artifact.json` reference. Compatible legacy content can be imported once into the canonical store. Correctness does not depend on NTFS hardlinks.

Queue JSON serialization now carries canonical artifact ID, fingerprint, path, and schema metadata. Existing queue-state authority and recovery formats remain compatible. In SQLite queue mode, compatibility snapshots already omit embedded history; the default JSON mode still retains its historical payload. Because SQLite, monthly JSONL, and compatibility JSON have different rollback/recovery roles, Phase 1 does not migrate or delete queue history. Choosing one authoritative long-term history store and supplying a verified migration/restore path remains Phase 2 work.

Legacy outputs and unknown files do not acquire destructive eligibility merely because the registry now exists. They remain `KEEP / BLOCKED_UNKNOWN_OWNER` until a separate evidence-backed migration.

## Tests Added

Regression coverage verifies:

- identical source/settings reuse one canonical artifact;
- changed source bytes, transcription settings, or alignment settings produce different IDs;
- missing/interrupted artifacts do not resolve and concurrent import commits one valid artifact;
- tagged and scanner reuse create no physical copy, while legacy runs still resolve;
- unknown, final, export, pending, and active-reference artifacts are blocked;
- regeneration requires real dependency evidence;
- dry-run planning never deletes and legacy ambiguity remains blocked;
- a successful validated job can retire its new raw, while failure, interruption, or missing validation retains it;
- reconciliation refuses ambiguous and basename-only matches.

## Validation Performed

- Backend unit/integration suite: **573 passed**, plus **50 subtests passed**; one non-failing warning.
- Focused storage/tagged/scanner regression rerun after the final no-copy compatibility change: **14 passed**.
- Frontend Vitest suite: **21 files / 95 tests passed**.
- Electron desktop tests: **12 passed**.
- Type checking and frontend production build: `tsc -b && vite build` passed.
- Python bytecode compilation and imports of the changed pipeline/storage modules passed.
- No separate lint command is configured by the project; `git diff --check` found no whitespace errors (only Git's existing LF-to-CRLF notices).
- Read-only `PRAGMA integrity_check` returned `ok` for the catalog, modular library, modular production, renderer, pilot, and artifact-registry databases.
- Post-reconciliation query confirmed zero stale modular-production output paths.

## Remaining Migration Work

- Migrate or reference-count the 5,000+ historical tagged runs before proposing transcript/checkpoint deletion.
- Backfill registry ownership and domain references for legacy working/output artifacts; unknown files must stay protected until then.
- Run a full resumable `D:\output_clips` metadata inventory during an operator-approved maintenance window and reconcile all active/final/export assignments.
- Verify complete regeneration dependency chains before allowing any modular render or variant to become a cleanup candidate.
- Migrate B-roll duplicates to a single physical content store with reversible role mappings.
- Decide queue-history authority, retention horizon, archive/checksum format, and tested restore path.
- Define retention approval and quarantine workflows for `REVIEW`, `REJECTED`, caches, builds, and terminal raw leftovers.
- Treat Git-history cleanup as a separate, coordinated repository operation; no history rewrite or garbage collection occurred.

## Phase 2 Cleanup Preconditions

Phase 2 must not begin until all of the following are true:

1. Every proposed artifact has a stable owner, lifecycle, and reference graph, or remains blocked.
2. Canonical imports are verified and all relevant legacy consumers resolve them correctly.
3. Production, export, final, assignment/distribution, active-job, retry, and pending references are reconciled.
4. Regenerable candidates have verified source, recipe, model/profile revision, transcript, and other required dependencies.
5. Ambiguous and missing records are resolved manually or excluded.
6. A fresh full inventory and dry-run plan are reviewed and approved by an operator.
7. Cleanup uses quarantine/recovery, bounded batches, durable tombstones, and post-operation database/media integrity checks.
8. Databases and operational metadata are backed up with a tested restore procedure.

Phase 2 was not started.
