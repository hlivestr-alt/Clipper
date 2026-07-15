# Long-term storage and desktop rollout

The catalog and queue migrations are deliberately feature-flagged. The default
runtime continues to read legacy files and write the legacy queue state until a
rollout stage is selected explicitly.

## Storage locations

- SQLite catalog: `working/catalog/clipper.sqlite3`
- Queue history: `working/queue_history/YYYY-MM.jsonl`
- Queue state compatibility snapshot: `working/video_queue_state.json`
- Migration backups: `working/queue_migration_backups/`

SQLite uses WAL mode, foreign keys, a busy timeout, and bounded retries. Queue
history records are append-only and carry checksums. The compatibility snapshot
contains active queue state only in schema v3; a schema v2 export with history
can be generated for rollback or external tools.

## Maintenance commands

Run commands from the repository root:

```powershell
python -m clipper_app.catalog_cli status
python -m clipper_app.catalog_cli backfill
python -m clipper_app.catalog_cli verify
python -m clipper_app.catalog_cli reconcile
python -m clipper_app.catalog_cli backup
```

Queue migration and compatibility export:

```powershell
python -m clipper_app.catalog_cli migrate-queue
python -m clipper_app.catalog_cli export-legacy-queue working\video_queue_state.v2.json
```

If catalog verification cannot be repaired in place, `rebuild` quarantines the
current database before creating and indexing a replacement:

```powershell
python -m clipper_app.catalog_cli rebuild
```

## Rollout stages

1. Backfill and shadow verification

   Keep `CLIPPER_CATALOG_MODE=legacy` and
   `CLIPPER_QUEUE_STORAGE_MODE=json`. Run `backfill`, then `verify`. The API
   status endpoint reports source counts, dirty sources, repairs, and shadow
   comparison results.

2. Queue dual write

   Set `CLIPPER_QUEUE_STORAGE_MODE=dual`. JSON remains the read source while
   every save also updates SQLite and the checksummed history journal. Compare
   active and historical counts across normal starts, stage transitions,
   completion, cancellation, and recovery.

3. Catalog read cutover

   Set `CLIPPER_CATALOG_MODE=catalog`. Scores, compliance, modules, overview,
   and output reads use the indexed read model. Revert the variable to `legacy`
   for immediate rollback; the legacy artifacts are not modified by catalog
   reads.

4. Queue read cutover

   Set `CLIPPER_QUEUE_STORAGE_MODE=sqlite`. SQLite becomes authoritative and an
   active-only compatibility snapshot is refreshed on lifecycle writes and at
   most every ten seconds during progress updates. Revert to `dual` or `json`
   and, if necessary, restore a migration backup or schema v2 export.

5. Push invalidation

   `CLIPPER_PUSH_INVALIDATION=1` is the default. `/api/events` supplies durable
   SSE event IDs, replay, reset notices after retention gaps, and heartbeats.
   The renderer falls back to polling if a live connection is unavailable.
   Set the flag to `0` to force polling-only operation.

## Desktop packaging

The Electron archive contains only the shell files. The compiled renderer is an
external packaged resource and `CLIPPER_STATIC_DIR` is injected into the managed
backend. The backend serves hashed assets with immutable caching and serves the
SPA entry with `no-cache`.

Build and verify the portable artifact with:

```powershell
cd new_app
pnpm test
pnpm build
pnpm desktop:test
pnpm desktop:portable
```

Before advancing a stage, require a clean catalog verification, zero shadow
count mismatches, valid queue journal checksums, passing automated suites, and a
successful portable startup/navigation smoke test.
