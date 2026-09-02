# Clipper Storage Phase 2A Migration

Migration ID: `phase2a_20260901T050000Z`
Execution date: 2026-09-01 (Asia/Shanghai)
Migration state: completed with zero failed actions

## Executive Summary

Phase 2A migrated the historical transcript corpus and safely reclaimed project storage. Every destructive action was gated by a durable plan, verified database/metadata backups, exact provenance and content evidence, committed canonical replacement, reference migration, resolver verification, and an append-only SQLite action journal.

| Measure | Before | After | Change |
| --- | ---: | ---: | ---: |
| Project logical size | 27,901,467,246 bytes | 19,733,690,776 bytes | -8,167,776,470 bytes |
| `working` logical size | 19,167,776,655 bytes | 10,976,420,700 bytes | -8,191,355,955 bytes |
| Observed C-volume free space | 190,113,701,888 bytes immediately before backups/execution | 199,609,372,672 bytes | **+9,495,670,784 bytes** |
| Proven-safe reclaimable | 0 B before ownership backfill | 10,296,885,181 bytes after backfill, before execution | 0 B after completed safe actions |

The project-size delta is smaller than the physical disk gain because hardlinks preserve both logical B-roll paths while sharing allocation, and because Phase 2A retained 542 MB of verified backups plus the journal, references, canonical manifests, and reports.

- Actual observed net bytes reclaimed: **9,495,670,784 bytes** (8.84 GiB), after backup and migration/report overhead.
- Proven duplicate payload eliminated: **10,296,885,181 bytes** (9.59 GiB) before that overhead.
- Transcript/checkpoint net canonicalization: **8,812,285,192 bytes**.
- B-roll physical deduplication: **1,484,599,989 bytes**.
- Raw-cut reclaim: **0 B**; no historical raw passed every safety condition.
- Modular-render reclaim: **0 B**; no exact duplicate render group was found.
- Development/build reclaim: **0 B**; release provenance was not sufficient for deletion.
- Historical run-local files retired: **10,311**.
- Run references migrated: **5,285**, pointing to **261** canonical artifacts.
- Candidates retained/skipped: **252** (102 transcripts and 150 raws).
- Migration action failures: **0**.
- Broken references found after migration: **0**.

The remaining audit-derived project potential is approximately 4.2–5.6 GiB, but it is not currently proven safe. The final ownership scan reports 16,364,461,449 logical bytes as unknown/unclassified and therefore protected. Hardlink aliases cause logical and unique-physical totals to differ.

`D:\output_clips` was **NOT** cleaned.
`D:\VOD` was **NOT** cleaned.
No UNKNOWN or ambiguous artifacts were deleted.

## Safety Backups

Before the first destructive action, Phase 2A created:

`working\storage_migrations\phase2a_20260901T050000Z\backups\`

The backup set contains seven SQLite database backups plus a ZIP of queue state/history, configuration, settings snapshots, storage reports, and every manifest used to approve a raw candidate. No raw candidate was ultimately approved. Every source database and backup returned `PRAGMA integrity_check = ok`; the ZIP passed `testzip`.

| Source | Backup bytes | SHA-256 |
| --- | ---: | --- |
| Artifact registry | 48,738,304 | `8bd9bc9d5dfbff6db494a8b1fb09dd380c04224dab2f3b210bd425a03c9aba22` |
| Catalog | 483,729,408 | `8b8253408b27a2965051c95aeafb84eb62627ff3cbb1bf2c05a2ebe33f0479c3` |
| Modular library | 4,726,784 | `43db1f7d183227244645ef8722629db07fa9b9d880f23d6224efb5edcfdb2eb2` |
| Modular planner | 729,088 | `b4dd95a7e535ee0b7cbbd2314efc63b06da70e146c7c11ee210a63d84c47af31` |
| Modular production | 1,388,544 | `4d7888beae5cf2c8da4b07c8093fbfe749d6c1a3f2444f0b2405d04c55764ec9` |
| Modular renderer | 217,088 | `7936bee47fe05228d7b1497055d2335ea2bdae80efdea0a62b8d466f6578bff0` |
| Modular variant pilot | 233,472 | `25b88e59d808f2c6b40022b235f9b92cebf7764c33e3dc871b2e19e790f167e7` |
| Metadata ZIP | 2,054,874 | `88d48b8741c33f160b313a45bc8431502b37045226a85b506634cefa6686e7cc` |

The migration directory also contains the pre-execution plan manifest and the authoritative `journal.sqlite3`. The report manifest is a compact export; the SQLite journal retains full evidence.

## Historical Transcript Migration

The migration examined the 5,387 top-level historical run transcripts identified by the original audit.

To qualify, a run needed all of the following:

- exact ownership in queue current/history state;
- transcript source path matching queue source identity;
- a valid historical `transcript.fingerprint.json` for the transcribe stage;
- source size and nanosecond mtime still matching the historical fingerprint;
- current source byte identity (full SHA-256 for small sources or the Phase 1 sampled identity for large VODs);
- required schema, transcriber, model, and language metadata;
- exact transcript SHA-256;
- for checkpoints, matching source/schema metadata and exact checkpoint SHA-256.

The system did not fabricate a current Phase 1 processing fingerprint. It created a distinct `legacy-content-and-provenance-v1` identity from the historical config hash, model/settings metadata, source byte identity, schema, and exact payload hashes. Because complete historical regeneration settings are unavailable, these 261 artifacts are pinned `PERMANENT_STATE` with `regenerable = false`.

Migration result:

| Item | Count | Bytes |
| --- | ---: | ---: |
| Migrated run transcripts | 5,285 | 5,823,630,236 |
| Migrated raw transcription checkpoints | 5,026 | 3,382,044,682 |
| Local files retired | 10,311 | 9,205,674,918 |
| Unique canonical artifacts | 261 | 393,389,726 payload bytes |
| Net transcript/checkpoint reclaim | — | **8,812,285,192** |
| Retained unverified runs | 102 | 462,204,979 |

Ninety-three retained runs refer to source VODs that are no longer present, and nine are not owned by queue state. They remain `LEGACY_UNVERIFIED_KEEP` with their original files intact.

Every canonical artifact was committed before any original was retired. All 261 canonical manifests and payload hashes were revalidated afterward; all 5,285 references resolved, and zero references were broken. Rerunning a completed journal is idempotent.

## Raw Cut Cleanup

The migration classified 150 historical raw cuts totaling 1,181,147,083 bytes. None satisfied the complete terminal-success proof, so none was deleted.

| Classification | Files | Bytes | Reason |
| --- | ---: | ---: | --- |
| `KEEP_ACTIVE` | 11 | 134,702,649 | Job not durably completed or metadata still references the raw |
| `KEEP_RECOVERY` | 35 | 966,446,046 | Failed/recovery state or exact manifest clip was not successful |
| `KEEP_MISSING_SUCCESSOR` | 8 | 20,302,376 | Required manifest/successor missing |
| `KEEP_UNKNOWN` | 1 | 59,696,012 | Run absent from authoritative queue ownership |
| `KEEP_INCONSISTENT` | 95 | 0 | Empty/inconsistent historical raw placeholders |

The classifier requires a completed queue job, no retry/active stage, one exact successful manifest row, committed compliance success, a non-empty successor, no registry reference, and no run-local JSON reference. A second identical classification is required immediately before deletion.

## Modular Render Deduplication

All media under `working\modular_renders` was strategically SHA-256 hashed. There were zero exact duplicate groups among the 50 retained media outputs, so no hardlinking or deletion occurred. Logical identities and downstream state remain unchanged.

## Legacy Render Cleanup

`working\style_renders` contains 379,506,947 bytes in 236 files; `working\style_render_cache` contains 295,523,802 bytes in 116 files. Current ownership, favorite/selected state, and complete regeneration evidence cannot be proven for every item. All were retained. No preview, test render, user-created render, or legacy media was deleted merely because its subsystem appears old.

## Trend Cache Cleanup

`working\trends` contains 3,723,097,434 bytes in 37,527 files. The major families are:

- `full_analysis_v2`: 2,231,605,271 bytes;
- source `media`: 1,203,365,546 bytes;
- `automatic_outputs`: 144,359,100 bytes;
- `analysis`: 128,992,429 bytes.

The source media is protected. The larger analysis tree mixes derived outputs with packaged runtime/dependency files and active analysis state; the current manifests do not prove a safe cache-only boundary. Consequently, Phase 2A retained the entire trend tree rather than risk removing runtime dependencies or approved analysis data.

## B-Roll Migration

Phase 2A found 112 exact cross-role SHA-256 groups between `assets\broll_intro` and `assets\product_broll`. Each group had one product-B-roll anchor and one intro alias.

- Physical copies before: 224.
- Physical allocations after: 112.
- Logical paths after: 224 (unchanged).
- Hardlink aliases created: 112.
- Physical bytes deduplicated: **1,484,599,989**.

Each replacement used a staged NTFS hardlink, same-volume check, exact hash verification, atomic path replacement, `samefile` verification, and registry commit. Visible filenames, product associations, role associations, deterministic ordering, and both legacy paths were preserved. The full frontend/backend suite, including product B-roll and variation selection tests, passed afterward.

## Queue History Optimization

Queue authority remains unchanged:

- JSON mode remains the current compatibility/current-state authority.
- SQLite remains the indexed state/history representation when its mode is enabled.
- Monthly checksummed JSONL remains immutable recovery/audit history.

`working\queue_history` is 16,764,977 bytes. Phase 2A did not compress or delete it because an authoritative history migration and restore procedure are not yet defined. Canonical transcript references are now stored per migrated run without rewriting immutable queue history payloads. Historical strings mentioning missing `transcript.json` paths were verified to be old error messages, not live file references.

## Logs

Pipeline logging is already bounded by `LockedSizeRotatingFileHandler` at 25 MiB with four backups. The current active log is 803,814 bytes and three rotations are approximately 25 MiB each. No active or diagnostic log was deleted.

## Build Artifacts

`new_app\dist` contains 598,019 bytes and `new_app\dist-desktop` contains 615,475,897 bytes. The latter includes current `0.4.1`, prior `0.4.0`, and `win-unpacked` outputs. Although the current build validates, release/signing provenance does not prove the prior executable redundant. Phase 2A therefore reports **DEVELOPMENT/BUILD RECLAIM: 0 B**. `node_modules` was not deleted.

## Git Findings

`.git` currently occupies 2,983,365,860 bytes. `.gitignore` continues to block new working data, logs, generated builds, caches, and reports intended to remain runtime-only. No Git object, pack, branch, commit, or history was deleted or rewritten. Git-history reclamation remains a separate explicit operation.

## External output_clips Inventory

The full external tree was inventoried using paths and filesystem metadata only; media payloads were not hashed.

| Lifecycle estimate | Files | Bytes |
| --- | ---: | ---: |
| `EXPORT` | 30,942 | 293,091,271,382 |
| `PENDING` | 82 | 1,196,730,363 |
| `REVIEW` | 73,881 | 1,174,952,722,922 |
| `REJECTED` | 39,258 | 484,179,230,480 |
| `UNKNOWN` | 284,548 | 359,286,238,456 |
| Total | 428,711 | 2,312,706,193,603 |

The preliminary path-tier potential (`REVIEW + REJECTED`) is **1,659,131,953,402 bytes**. It is not a cleanup authorization: ownership, assignment, user selection, and retention eligibility remain unverified. Proven-safe external reclaim is **0 B**.

`D:\output_clips` was NOT cleaned.

## VOD Inventory

`D:\VOD` contains 285 source files totaling 937,249,398,486 bytes. The scan used metadata only. Every VOD remains `SOURCE`; age did not create cleanup eligibility, and proven-safe reclaim is 0 B.

`D:\VOD` was NOT cleaned.

## Files Deleted

The authoritative path-level list is in `reports\storage-phase2a-manifest.json`; full evidence is in the migration SQLite journal.

| Category | Files retired | Bytes retired | Net physical reclaim |
| --- | ---: | ---: | ---: |
| Historical run-local transcripts | 5,285 | 5,823,630,236 | Included below |
| Historical run-local checkpoints | 5,026 | 3,382,044,682 | Included below |
| Transcript/checkpoint total | **10,311** | 9,205,674,918 | **8,812,285,192** after canonical payloads |
| Historical raw cuts | 0 | 0 | 0 |
| Modular/legacy/trend media | 0 | 0 | 0 |
| Build artifacts | 0 | 0 | 0 |
| External media | 0 | 0 | 0 |

The B-roll operation replaced 112 duplicate physical allocations with hardlinks while preserving all 112 logical alias paths; these are counted as deduplicated allocations, not deleted user-visible files.

The migration also retired 456 known test-only artifact-registry rows after backing up the registry. Every row pointed into the Windows temporary directory, the path was missing, declared data totaled only 15,273 bytes, and active reference count was zero. No media file was deleted by this metadata repair.

## Files Retained and Why

- 102 transcript/checkpoint runs (462,204,979 bytes): missing VOD or missing queue ownership.
- All 150 raw cuts (1,181,147,083 bytes): active, recovery, inconsistent, missing-successor, or unknown evidence.
- All modular renders: no exact duplicate groups and downstream logical identities remain relevant.
- All style renders/cache: user/selection and regeneration ownership unresolved.
- All trends data: source/runtime/derived boundary is not sufficiently proven.
- Queue JSON, SQLite history, and JSONL: audit/recovery authority migration not defined.
- All logs: current bounded retention is functioning.
- All builds and `node_modules`: release provenance or dependency role requires retention.
- All Git data: history rewrite expressly out of scope.
- All external output and VOD media: inventory-only scope.

Unknown and ambiguous data remained `KEEP` throughout.

## Migration Failures / Skips

There were zero failed actions and no interrupted retirement.

Skipped/retained candidates totaled 252:

- 102 `LEGACY_UNVERIFIED_KEEP` transcript runs;
- 11 `KEEP_ACTIVE` raws;
- 35 `KEEP_RECOVERY` raws;
- 8 `KEEP_MISSING_SUCCESSOR` raws;
- 1 `KEEP_UNKNOWN` raw;
- 95 `KEEP_INCONSISTENT` zero-byte raw placeholders.

## Validation

- Backend unit/integration suite: **581 passed**, plus **50 subtests passed**; one non-failing Starlette deprecation warning.
- Frontend Vitest: **21 files / 95 tests passed**.
- Electron desktop tests: **12 passed**.
- TypeScript and production frontend build: `tsc -b && vite build` passed.
- Python compilation/import and FastAPI creation passed; the app exposed 97 routes.
- Queue JSON loaded 155 videos successfully.
- Historical transcript smoke load through a canonical reference succeeded.
- All 261 canonical artifacts, 5,285 run references, 102 retained originals, and 112 B-roll hardlinks were verified.
- Catalog, modular library/planner/production/renderer/pilot, artifact registry, and migration journal all returned `PRAGMA integrity_check = ok`.
- Modular production retained 216 rows with zero stale output paths.
- Modular-library transcript cache retained 111/111 existing paths.
- Artifact registry contains 373 real artifacts and 5,509 active references, with zero temporary-test paths.
- `git diff --check` found no whitespace errors; only the repository's existing LF-to-CRLF notices appeared.
- The project defines no separate lint command; TypeScript checking, Python compilation, and the complete test suites provide the configured static/build validation.

## Remaining Phase 2B Work

- Manually resolve or restore ownership/source evidence for the 102 retained transcript runs.
- Reconcile failed/recovery raw jobs and successors before reconsidering any of the 1.18 GB raw corpus.
- Define authoritative selected/favorite/regeneration state for legacy style media.
- Separate trend runtime/source material from manifest-owned derived caches.
- Define and test queue-history authority, compression, archival, and restore behavior.
- Establish signed release provenance before deleting old build outputs.
- Build assignment/final/user-selection ownership for the external REVIEW/REJECTED corpus; its 1.659 TB estimate is potential only.
- Keep VOD deletion and Git-history rewriting as separately authorized operations.

Phase 2A does not automatically proceed to external corpus cleanup.
