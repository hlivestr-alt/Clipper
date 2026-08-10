# Long-Term Storage and Desktop Rollout

The catalog and queue migrations are deliberately feature-flagged. The current defaults still read legacy output artifacts and use JSON queue state. Do not change a mode without completing its verification and rollback gate.

## Storage Map

- Shared SQLite catalog: `working/catalog/clipper.sqlite3` or `CLIPPER_CATALOG_PATH`.
- Queue history journals: `working/queue_history/YYYY-MM.jsonl`.
- Queue compatibility snapshot: `working/video_queue_state.json`.
- Queue migration backups: `working/queue_migration_backups/`.
- Control-job metadata: `working/app_control_jobs/`.
- Control-job results: `working/app_control_job_results/`.
- Control audit: `working/app_control_audit.jsonl`.
- Settings overrides: `working/settings_overrides.json`.
- TikTok encrypted credentials: `working/secrets/tiktok_oauth.tokens` by default; this is separate from the catalog.
- WhatsApp delivery database: `D:\output_clips\export_batches_whatsapp\_whatsapp_state.sqlite3` by current configuration; this is a separate authoritative store.

The shared catalog currently uses schema version 12. It contains rebuildable sources/revisions/snapshots/repairs; outputs, clips, scores, compliance, modules, and export status; optional queue active/history state; durable change events; and TikTok snapshots, hashtags, videos, downloads, media links, fingerprints, and patterns.

SQLite uses WAL mode, foreign keys, a busy timeout, and bounded retries. Queue journals are append-only and checksummed. In SQLite queue mode, the compatibility JSON snapshot contains active state in schema version 3; a schema-version-2 export with embedded history can be generated for rollback or external legacy tools.

## Current Default Modes

```text
CLIPPER_CATALOG_MODE=legacy
CLIPPER_QUEUE_STORAGE_MODE=json
CLIPPER_PUSH_INVALIDATION=1
```

The catalog can be populated even while legacy reads remain authoritative. On 2026-08-10, `catalog_cli status` reported schema 12, integrity `ok`, zero recorded repair rows, runtime queue mode `json`, and stored successful trend fingerprints/patterns. Treat counts and revision numbers as operational state, not documentation contracts.

## Maintenance Commands

Run from the repository root:

```powershell
python -m clipper_app.catalog_cli status
python -m clipper_app.catalog_cli backfill
python -m clipper_app.catalog_cli verify
python -m clipper_app.catalog_cli reconcile
python -m clipper_app.catalog_cli backup
```

Queue migration and legacy export:

```powershell
python -m clipper_app.catalog_cli migrate-queue
python -m clipper_app.catalog_cli export-legacy-queue working\video_queue_state.v2.json
```

If verification cannot be repaired in place, `rebuild` quarantines the current database before creating and indexing a replacement:

```powershell
python -m clipper_app.catalog_cli rebuild
```

Always record the backup/export location and verify the replacement before deleting any quarantine or migration backup.

## Rollout Stages

1. **Backfill and shadow verification**

   Keep catalog reads in `legacy` and queue storage in `json`. Run `backfill`, then `verify`. Check source counts, dirty/repair rows, integrity, and shadow comparisons. A historical zero-mismatch result is not proof that current artifacts are still synchronized.

2. **Queue dual write**

   Set `CLIPPER_QUEUE_STORAGE_MODE=dual`. JSON remains the read authority while every lifecycle write also updates SQLite and the monthly history journal. Exercise registration, progress, retries, stage failure, completion, cancellation, pause/restart, and recovery. Verify both active state and history checksums.

3. **Catalog read cutover**

   Set `CLIPPER_CATALOG_MODE=shadow` for a soak, then `catalog` after comparisons remain clean. Scores, compliance, modules, overview, and supported output reads use the indexed model. Revert immediately to `legacy` if a query or count disagrees; catalog reads do not modify legacy artifacts.

4. **Queue read cutover**

   Set `CLIPPER_QUEUE_STORAGE_MODE=sqlite`. SQLite becomes authoritative and refreshes the active-only JSON compatibility snapshot on lifecycle writes and at most every ten seconds during progress updates. Roll back to `dual` or `json` with a verified backup/schema-2 export if needed.

5. **Push invalidation**

   `/api/events` is enabled by default and provides durable SSE IDs, replay, reset notices after retention gaps, and heartbeats. The React client falls back to polling. Set `CLIPPER_PUSH_INVALIDATION=0` only to force polling during diagnosis.

## Job-Result Migration

Control jobs use schema-version-2 metadata and separate bounded result files. `CLIPPER_MIGRATE_JOB_STORAGE=1` atomically migrates legacy embedded results, retains rollback backups, and applies current result expiration/truncation rules. Electron and `run_new_app.ps1` enable this migration on startup. This is independent of catalog and queue cutover.

## Desktop Packaging Gate

The Electron archive contains the shell and compiled renderer only. The renderer is packaged under `resources/renderer`; `CLIPPER_STATIC_DIR` points the managed backend at it.

```powershell
cd new_app
pnpm.cmd test
pnpm.cmd build
pnpm.cmd desktop:test
pnpm.cmd desktop:portable
```

The current artifact is `new_app/dist-desktop/Clipper-0.4.1-portable.exe`. Before a rollout, require passing automated suites, a clean catalog verification, valid queue journal checksums, proven rollback exports, and a real portable startup/navigation/control smoke against the intended Python/project runtime.
