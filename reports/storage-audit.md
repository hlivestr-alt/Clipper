# Clipper Storage Audit

Audit date: 2026-08-31 (Asia/Shanghai)  
Project root: `C:\Data\Clipper Ai Trends`  
Method: read-only filesystem metadata scan, targeted SHA-256 hashing, read-only SQLite queries, and source-code tracing. Sizes are logical file sizes and use binary units unless marked as raw bytes.

> **Phase 1 implementation status (2026-09-01):** The architecture work described in this audit has been implemented for new operations. Canonical transcript references now prevent new tagged-run transcript/checkpoint copies, managed artifacts and publish transitions are recorded, and all 119 strongly identifiable stale modular-production paths were reconciled without deleting media. The original measurements and findings below remain the audit baseline. See `reports\storage-phase1-implementation.md` and `reports\storage-reconciliation.md`. Historical bulk cleanup was **not** performed; legacy/untracked data remains protected and the reclamation path is dry-run only.

> **Phase 2A migration status (2026-09-01):** Historical project migration is complete for strongly proven candidates. A verified, resumable journal migrated 5,285 historical runs to 261 pinned canonical transcript artifacts, retired 10,311 redundant run-local transcript/checkpoint files, and replaced 112 exact cross-role B-roll copies with verified hardlink aliases. All 102 unverified transcript runs and all 150 historical raw cuts were retained. The final journal snapshot observed 9,495,670,784 additional free bytes after accounting for backups and migration metadata. `D:\output_clips` and `D:\VOD` were inventory-only and were not cleaned. See `reports\storage-phase2a-migration.md` and `reports\storage-phase2a-manifest.json`.

## Executive Summary

| Measure | Finding |
| --- | --- |
| **TOTAL PROJECT SIZE** | **25.16 GiB** (27,011,072,517 bytes; 97,970 files at inventory time) |
| **WORKING DIRECTORY SIZE** | **17.81 GiB** (19,118,025,193 bytes; 77,152 files; 70.8% of the project) |
| **ESTIMATED IMMEDIATELY RECLAIMABLE SPACE** | **About 2.2 GiB of low-risk rebuildable development/test/runtime material**, provided no build, forensic review, or one-off analysis must be preserved. If “immediate” means no operator decision and no reference-aware migration, the safe answer is **0 B**. Nothing was deleted in this audit. |
| **ESTIMATED RECLAIMABLE SPACE WITH RETENTION POLICIES** | **About 13.8–15.2 GiB inside the project.** The lower bound includes canonical transcript storage, terminal raw-cut cleanup, completed one-off analysis retention, legacy style-output retention, and rebuildable development artifacts. The upper bound also deduplicates the two active B-roll trees through a shared content store/hardlinks. It excludes risky Git-history rewriting. |
| **MOST IMPORTANT STORAGE LEAK / CAUSE** | Tagged reruns create a new working directory and explicitly copy both `transcript.json` and `transcript.raw_checkpoint.json`. Across 5,387 and 5,123 files respectively, only 195/196 distinct contents exist. **8.44 GiB is byte-for-byte duplicate transcript data.** |
| **IS DATA DUPLICATION SIGNIFICANT?** | **Yes.** Targeted hashing proves at least **11.0 GiB** of exact duplicate excess in the project when all transcript files and other duplicate files at least 1 MiB are combined. Not all of this is currently safe to delete because both paths or historical records may be referenced. |

The configured media roots outside the project are even larger and are important to the lifecycle analysis, but are not included in the 25.16 GiB project total:

- `D:\VOD`: 285 source files, **872.97 GiB** total; average 3.06 GiB, median 2.05 GiB, maximum 6.41 GiB.
- `D:\output_clips`: 96,107 directories, 428,711 files, **2.10 TiB** (2,312,706,193,603 bytes).
- Together these external roots account for about 3.0 TiB. The final-output root alone is substantially larger than the source root, consistent with six variants per base clip and indefinite final/rejected-output retention.

### Top 10 project storage consumers

These are non-overlapping path families; nested detail is shown later.

| Rank | Path / family | Size | Files | Main reason |
| ---: | --- | ---: | ---: | --- |
| 1 | `working\<dated source/run folders>` | 10.80 GiB | 38,774 | Per-run transcripts/checkpoints and stage metadata; some abandoned raw cuts |
| 2 | `working\trends` | 3.47 GiB | 37,527 | Downloaded trend media, analysis jobs/contact sheets, and a bundled one-off Python runtime |
| 3 | `assets` | 3.12 GiB | 376 | B-roll/source assets; 1.39 GiB is duplicated between two active role-specific trees |
| 4 | `.git` | 2.78 GiB | 514 | Git packs retain historical raw cuts, logs, and media blobs |
| 5 | `working\modular_renders` | 1.55 GiB | 62 | 50 retained modular base MP4s plus 12 reports |
| 6 | `new_app` | 1.13 GiB | 17,275 | Electron `node_modules` plus packaged desktop builds |
| 7 | `working\modular_variant_pilot` | 526.03 MiB | 39 | 36 retained pilot variants plus reports |
| 8 | `working\catalog` | 461.32 MiB | 2 | Shared SQLite catalog and sidecar; metadata, not media binaries |
| 9 | `working\style_renders` | 361.93 MiB | 236 | Historical style-render outputs; current source contains no owning service |
| 10 | `working\style_render_cache` | 281.83 MiB | 116 | Historical rendered-media cache with no current retention/size bound |

## Disk Usage Breakdown

| Top-level path | Size | Files | Classification |
| --- | ---: | ---: | --- |
| `working` | 17.81 GiB | 77,152 | Mixed runtime state, caches, generated media, databases, job history |
| `assets` | 3.12 GiB | 376 | Important source media and documents |
| `.git` | 2.78 GiB | 514 | Development history; not runtime data |
| `new_app` | 1.13 GiB | 17,275 | Frontend source, dependencies, builds, installers |
| `dataset` | 177.45 MiB | 1,993 | Development/training data |
| root `pipeline.log*` | 75.78 MiB | 4 | Bounded operational logs |
| `runs` | 34.64 MiB | 62 | Development/model-run artifacts |
| `__pycache__` | 8.46 MiB | 257 | Rebuildable Python bytecode |
| `highlight_phrases.json` | 6.42 MiB | 1 | Learned/runtime configuration state; retain unless deliberately migrated |
| `models` + `yolov8n.pt` | 12.19 MiB | 2 | Re-downloadable model weights, but required for offline operation |
| `artifacts` | 5.42 MiB | 39 | Test/visual-review artifacts |
| `.pytest_cache` | 2.78 MiB | 5 | Rebuildable test cache |
| `clipper_app`, scripts, source/tests/docs | under 8 MiB combined, excluding generated caches | — | Application source |
| `temp_ass` | 1.13 MiB | 25 | Subtitle scratch/test material |

The `.git` object database consists of 2.76 GiB in three packs. `git count-objects -vH` also reported one 1,012 KiB temporary garbage object. Large historical blobs include a 77,002,935-byte `pipeline.log`, `assets/variation_preview/raw_cut_preview.mp4`, and old `working/.../raw_cuts/*.mp4`. Reclaiming Git history would require a coordinated history rewrite and garbage collection; it is not an ordinary cache cleanup.

## Working Directory Breakdown

### Dated production working folders

There are 5,490 dated first-level directories: 5,436 tagged run directories and 54 untagged/base directories. The tagged runs cover 155 source stems. The mean is 35.1 run folders per source, the median is 6, and the maximum is 180 for one source.

| Path family / second-level role | Size | Files / dirs | Last meaningful modification | Purpose and lifecycle |
| --- | ---: | ---: | --- | --- |
| `working\*_run_*` | 10.50 GiB | 38,349 files / 5,436 dirs | 2026-08-26 | Versioned/rerun stage state. Historically referenced by queue run history. Terminal-run retention is absent. |
| `working\<source stem>` | 307.50 MiB | 425 files / 54 dirs | 2026-06-24 | Untagged/base cache used as a transcript reuse source. Keep the canonical copy while its VOD or dependent scanner record exists. |
| `...\transcript.json` | 5.69 GiB | 5,387 | 2026-08-24 | Aligned transcript used by moment detection, scoring, compliance, scanner bridge, resume and retry. Canonical content is important; per-run copies are not intrinsically unique. |
| `...\transcript.raw_checkpoint.json` | 3.32 GiB | 5,123 | 2026-08-24 | Raw transcription recovery checkpoint used to avoid repeating Whisper after alignment failure. Useful until a canonical aligned transcript is validated; unnecessary as one copy per rerun. |
| `...\raw_cuts` | 1.10 GiB | 150 files / 46 dirs | 2026-08-25 | FFmpeg intermediates. Normal success paths remove them. Surviving files are interrupted/legacy leak evidence. |
| `...\product_detections.json` | 384.51 MiB | 4,292 | 2026-08-26 | YOLO stage cache; regenerable from source VOD plus settings/model, but needed for fast resume. |
| `...\moments.json` | 214.23 MiB | 4,506 | 2026-08-26 | LLM moment cache; regenerable but costly and needed for resume/reproducibility. |
| `...\module_candidates.json` | 99.23 MiB | 839 | 2026-06-18 | Legacy modular extraction output; current modular scanner has replaced this path. Retain only while legacy records require it. |
| fingerprints and detection metadata | 11.53 MiB | 13,090 | 2026-08-26 | Small integrity/cache-compatibility state. Retain with the corresponding canonical stage artifact. |

Code evidence:

- `video_queue.py:1220-1224` derives a new working/output directory from the VOD stem plus `working_tag`/`output_tag`.
- `video_queue.py:2244-2285` reuses a transcript by `shutil.copy2` into the new tagged run and copies the raw checkpoint too.
- `transcriber.py:24-82` reads the raw checkpoint for resume and writes the final aligned transcript; `transcriber.py:20` names the checkpoint.
- `main.py:1957-1961` creates the per-run working directory; `main.py:2196-2203` creates `raw_cuts` and resume state.
- `main.py:338-339` and `main.py:398-399` delete a raw cut after cut-only or normal rendering completes. Early process termination and historical code paths leave it behind.
- `video_queue.py:2345-2421` “cleanup stale queue” changes queue stage state only. It does not remove working files.

### Meaningful special directories and files under `working`

| Path | Size | Files | Purpose | Active / referenced? | Required or rebuildable? | Retention candidate? |
| --- | ---: | ---: | --- | --- | --- | --- |
| `trends\full_analysis_v2` | 2.08 GiB | 35,431 | Completed 530-video one-off full editing analysis | Manifest says completed 2026-07-17; no current source references its path | `_runtime` (834.18 MiB) rebuildable; 1.23 GiB contact-sheet JPGs regenerable from source trend media; reports/manifests worth retaining | Yes: retain compact reports/manifests; expire runtime and per-video derived frames after QA sign-off |
| `trends\media` | 1.12 GiB | 326 | Approved/downloaded TikTok/trend source media | Referenced by catalog trend tables and analysis | Source media; not cache while approved links depend on it | Configurable source-media retention after reference check |
| `trends\automatic_outputs` | 137.67 MiB | 58 | Historical automatic style outputs | No current source path reference found; many hash-match style cache/render files | Regenerable/historical final candidates | Retain only selected/final results; otherwise expire after migration |
| `trends\analysis` | 123.02 MiB | 1,430 | Current configured trend analysis root | `config.py:577`, analysis/catalog records | Jobs are regenerable; reports/fingerprints are useful | Bound job/contact-sheet cache by age/bytes |
| `trends\analysis_m6_validation` | 12.93 MiB | 255 | Validation/test analysis corpus | Historical validation, no configured current root | Rebuildable test artifact | Yes, with test-artifact retention |
| `modular_renders` | 1.55 GiB | 62 | 50 base compositions and 12 reports | All 12 runs and 50 items are `completed`; DB paths all exist; some are bases for pilot/production lineage | Base media regenerable from source + immutable planner manifest; currently required by pilot lineage and rerender-free playback | Only after all dependent variant/production refs are resolved; e.g. 30–90 days after promotion |
| `modular_variant_pilot` | 526.03 MiB | 39 | 36 variant outputs and 3 reports | 2 completed runs, 1 failed; 6 completed and 4 failed base items; all 36 output paths exist | Review artifacts; regenerable from base + profile + transcript bridge | Short pilot retention after review/promotion, preserving report/recipe |
| `catalog` | 461.32 MiB | 2 plus WAL/SHM | Shared application catalog | Active persistent state | Required; rebuildable only partly and at cost; contains history and user/project metadata | Never delete casually; table-level retention and backup first |
| `style_renders` | 361.93 MiB | 236 | Historical style-render outputs/logs | 97 catalog jobs refer to this family; current source has no style-render owner | Outputs may be regenerable from stored recipes, but current implementation cannot guarantee it | Migration first, then selected/final vs expired policy |
| `style_render_cache` | 281.83 MiB | 116 | Historical rendered-media cache | No current code reference found; exact duplicates exist elsewhere | Regenerable cache | Strong bounded-cache candidate after legacy migration |
| `modular_scanner\transcripts` | 96.86 MiB | 112 | Scanner-owned transcript copies | 111 media sources/transcript rows; scanner and variant transcript bridge use them | Required for scanner reproducibility and variant bridge; source-copy duplication is deliberate under current design | Deduplicate through canonical artifact IDs, not manual deletion |
| `modular_renderer_seek_benchmark` | 69.06 MiB | 7 | Old/new renderer seek benchmark | Development-only | Rebuildable | Retain latest benchmark report, expire MP4s after 14–30 days |
| `test_render_20260720` | 50.73 MiB | 8 | Render test fixture/output | Development-only | Rebuildable if source remains | Yes, short test retention |
| `video_queue_state.json` | 26.84 MiB | 1 | Default schema-2 compatibility/authority snapshot | Queue is stopped; contains 155 videos and 5,440 run-history records | Important operational state and rollback/export contract | Keep current state; compact old history to canonical DB/journal after migration |
| queue state backups/migration backup | 67.78 MiB | 3 | Manual/migration recovery snapshots | Historical | Important only for rollback window | Retain latest known-good + 30–90 day migration window |
| `queue_history` | 15.99 MiB | 6 | Checksummed monthly immutable JSONL history | Mirrored by 4,897 catalog history rows | Recovery/audit state | Compress/archive by month; define legal/operational horizon |
| `queue_supervisor_launch.log.[1-3]` | 30.00 MiB | 3 | Rotated supervisor logs | Not active state | Diagnostics only | Existing rotation is working; retain 3 backups as configured |
| `variation_previews` | 5.73 MiB | 60 | Generated profile previews | UI cache | Regenerable | LRU/age/byte bounded |
| `_module_extraction_state` | 5.41 MiB | 50 | Legacy extraction completion/dedupe state | Legacy only; current modular scanner is SQLite-backed | Preserve until legacy migration complete | Then archive/delete with migration record |
| `modular_library.sqlite3` | 4.51 MiB | 1 plus sidecars | Scanner sources, scans, chunks, segments, rejections, batches | Active persistent modular library | Must retain | Generation/rejection retention only via explicit archival policy |
| `modular_production.sqlite3` | 1.32 MiB | 1 plus sidecars | Production orchestration/lineage | Active persistent state | Must retain | Row retention only after export lineage is durable |
| `modular_planner.sqlite3` | 712 KiB | 1 plus sidecars | Draft/approved plans and immutable manifests | Active persistent planner state | Must retain | Archive drafts only through product workflow |
| `modular_variant_pilot.sqlite3` | 228 KiB | 1 plus sidecars | Pilot run/item/output lineage | Active until pilot migration | Must retain with outputs | Retain metadata longer than media |
| `modular_renderer.sqlite3` | 212 KiB | 1 plus sidecars | Renderer run/item state and output paths | Active for playback/recovery | Must retain | Retain metadata longer than media |
| `app_control_jobs`, `app_control_job_results`, audit | under 117 KiB current | 19+ | UI/background control-job state/results/audit | Current service-owned state | Retention already implemented | Existing 30-day/2,000 metadata and 7-day/250 MiB result bounds are appropriate |
| `secrets` | 829 B | 2 | Encrypted TikTok OAuth/token state | Active credential state | Must retain and back up securely | Never include in generic cleanup |

## Largest Files

The audit calculated the largest 100 project files. The top 25 explain most single-file pressure; the complete top-100 inventory follows in the appendix.

| Rank | Size | Path | Meaning |
| ---: | ---: | --- | --- |
| 1 | 2.03 GiB | `.git\objects\pack\pack-f0cb...pack` | Git history pack |
| 2 | 585.05 MiB | `.git\objects\pack\pack-d9be...pack` | Git history pack |
| 3 | 461.32 MiB | `working\catalog\clipper.sqlite3` | Shared catalog |
| 4 | 221.59 MiB | `new_app\node_modules\.pnpm\...\electron.exe` | Electron dependency copy |
| 5 | 221.59 MiB | `new_app\dist-desktop\win-unpacked\Clipper.exe` | Packaged app copy |
| 6 | 167.47 MiB | `.git\objects\pack\pack-dd22...pack` | Git history pack |
| 7 | 145.01 MiB | `new_app\dist-desktop\Clipper-0.4.0-portable.exe` | Old installer |
| 8 | 127.83 MiB | `working\trends\full_analysis_v2\_runtime\...\libpaddle.pyd` | One-off bundled runtime |
| 9 | 88.36 MiB | `...\_runtime\paddle\libs\mklml.dll` | One-off bundled runtime |
| 10 | 87.24 MiB | `new_app\dist-desktop\Clipper-0.4.1-portable.exe` | Current/another installer |
| 11 | 85.65 MiB | `...\_runtime\cv2\cv2.pyd` | One-off bundled runtime |
| 12 | 56.93 MiB | `working\...async_smoke...\raw_cuts\...mp4` | Orphaned smoke-run raw intermediate |
| 13–25 | 39–47 MiB each | raw cuts, modular base MP4s, runtime DLLs, style renders | Generated/intermediate media and runtime binaries |

## Storage by File Type

| Extension/category | Size | Files | Interpretation |
| --- | ---: | ---: | --- |
| `.json` | 9.99 GiB | 47,412 | Dominated by repeated transcripts/checkpoints and stage metadata |
| `.mp4` | 5.36 GiB | 881 | Raw cuts, modular/style outputs, trend media, test/preview media |
| `.pack` | 2.77 GiB | 3 | Git history |
| `.mov` | 2.77 GiB | 224 | Source B-roll assets, heavily duplicated across role directories |
| `.jpg` | 1.47 GiB | 4,538 | Mostly 1.23 GiB of one-off trend-analysis contact sheets |
| `.exe` | 697.89 MiB | 52 | Electron runtimes/installers and bundled runtime tools |
| `.sqlite3` | 468.28 MiB | 6 | Persistent metadata databases |
| `.dll` | 346.26 MiB | 45 | Electron and one-off analysis runtime |
| `.pyd` | 267.63 MiB | 160 | One-off Python runtime |
| `.pyc` | 174.18 MiB | 10,734 | Bundled runtime and Python cache |
| `.png` | 106.16 MiB | 151 | UI/test/preview images |
| audio (`.mp3`, `.wav`, etc.) | 84.84 MiB | 52 | BGM/SFX and extracted analysis audio |
| subtitle (`.ass`, etc.) | 1.13 MiB | 31 | Small generated subtitle scripts |
| log filename family (`*.log*`) | 107.29 MiB | 137 | Root pipeline and working supervisor/test logs |

Media-category totals: video 8.16 GiB, image 1.58 GiB, audio 84.84 MiB, SQLite main files 468.28 MiB. The apparent 10.01 GiB JSON/JSONL total is the central project-local growth problem.

## Pipeline Data Lifecycle

1. **Source VOD** — read from `D:\VOD` (`config.py:18`). It is external to the project and retained indefinitely today.
2. **Per-run directory** — `video_queue.py:1220-1224` and `main.py:1957-1961` create `working\<source>_<run tag>`.
3. **Transcription** — `transcriber.py` writes a raw recovery checkpoint and final aligned transcript. A tagged rerun copies both from an earlier compatible run (`video_queue.py:2276-2282`).
4. **Moment and product analysis** — `moment_detector.py:472-587` writes/reads `moments.json`; `vision_scanner.py:255-409` writes/reads `product_detections.json`; stage fingerprints guard cache reuse.
5. **Variation expansion** — configuration requests six variants (`config.py:610-616`). The expanded moments become separate clip jobs and separate MP4s.
6. **Raw cut** — `_build_clip_job` assigns `working\...\raw_cuts\<clip>_raw.mp4` (`main.py:108-133`). Normal success removes it (`main.py:338-339`, `398-399`).
7. **Rendered output** — final clips, manifest, render state, scores, and compliance live under external `D:\output_clips\<run>`.
8. **Scoring tier** — `clip_scorer.py:3468-3530` moves rendered files into `export_ready`, `review_needed`, or `rejected`; it does not delete rejected media.
9. **Export packaging** — `export_packager.py:445-460` and `1107-1117` move export-ready clips into batch/pending destinations and update manifest/score paths. It generally avoids an extra copy.
10. **Distribution** — other delivery code can copy into delivery-specific destinations (for example `whatsapp_media.py:1381` uses `copy2`), so delivery retention must be audited with the destination policy as well.

No general lifecycle manager connects these stages. Every producer chooses its own path and most terminal outputs remain until an operator removes them.

## Clip and Variant Multiplication

Observed recent modular production provides a clean measurement:

- 30 completed base renders consumed 997,055,749 bytes: **31.70 MiB average per base**.
- 180 variants consumed 2,745,990,308 bytes: **14.55 MiB average per variant**.
- Exactly **6 variants per base** were recorded.
- Base plus variants consumed **119.0 MiB per base**: a **3.74× media multiplier relative to the retained base MP4**.
- A 20-base run therefore produced about **2.32 GiB** of base + variant media.

The latest 20-base external output retained 120 compliance JSON files, 120 `export_ready` clips (1.63 GiB), and 12 `review_needed` clips (86.4 MiB). Scoring moved clips out of the original `v0`–`v5` paths rather than copying them. The production SQLite rows still point to 119 old pre-move paths, while the files exist under `export_ready`; this is a reference-integrity defect that a cleanup system must not misinterpret as “missing and safe.”

Rejected/review variants are not automatically deleted. Modular base MP4s also remain after downstream variants complete. Pilot variants remain after review. Thus multiplication persists even when intermediate FFmpeg scratch files are cleaned correctly.

### Scaling estimate

Assumptions: observed 20 bases per VOD, six variants per base, observed media averages above, and the current average source VOD of 3.06 GiB. Actual output varies with moment count, clip duration, bitrate, compliance, and export selection.

| VOD count | Source VODs | Base + six variants | Combined retained media | Project-local rerun metadata at current mean |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 3.06 GiB | 2.32 GiB | 5.38 GiB | ~71 MiB across mean 35 run folders; canonical design needs only a few MiB |
| 10 | 30.63 GiB | 23.23 GiB | 53.86 GiB | ~710 MiB |
| 100 | 306.27 GiB | 232.31 GiB | 538.58 GiB | ~6.9 GiB |
| 1,000 | 2.99 TiB | 2.27 TiB | 5.26 TiB | ~69 GiB |

The external roots already show this effect in practice: 872.97 GiB of source VODs and 2.10 TiB of final/output material, before counting project-local bases/caches.

## Databases and Persistent State

All six databases passed `PRAGMA quick_check` with `ok`. Each uses WAL mode. Every observed `-wal` file was zero bytes and every `-shm` was 32 KiB, so no abnormal live WAL accumulation exists. Freelist counts are tiny (catalog 62 pages, production/pilot 3 pages each), so `VACUUM` would reclaim only about 0.25 MiB now and is not a meaningful solution.

| Database | Main size | Tables / significant counts | Largest logical payloads | Media blobs? | Retention assessment |
| --- | ---: | --- | --- | --- | --- |
| `working\catalog\clipper.sqlite3` | 461.32 MiB | 48 tables; 231,460 clips; 13,456 score rows; 12,124 compliance results; 40,425 trend transitions; 39,793 change events; 4,897 queue history rows | clips ~152 MiB logical; score records ~48 MiB; style recipe versions ~42 MiB; style recipes ~27 MiB; analysis results ~21.5 MiB | No media. One BLOB column contains 28,054,637 bytes total of compressed JSON payload, max 2,357 bytes/row. | Important catalog/history. Some tables are current projections; others are immutable history or legacy style state. Add per-domain retention, not file deletion. |
| `working\modular_library.sqlite3` | 4.51 MiB | 111 sources/transcripts; 135 scans; 434 chunks; 1,858 segments; 2,234 rejections | segments, rejections, chunk response JSON | No | Persistent scanner library and resume state. Keep current generations; archive old failed/obsolete generations after lineage check. |
| `working\modular_production.sqlite3` | 1.32 MiB | 4 jobs; 37 items; 216 variants | variant lineage JSON ~1.0 MiB | No | Critical production lineage. Contains stale pre-move paths; reconcile before cleanup. |
| `working\modular_planner.sqlite3` | 712 KiB | 18 runs (11 approved, 7 draft); 11 manifests; 92 compositions; 276 items | immutable manifest/item JSON | No | Planner state; approved manifests must never be deleted while renderer/production refers to them. |
| `working\modular_variant_pilot.sqlite3` | 228 KiB | 3 runs; 10 items; 36 outputs | transcript/profile diagnostics | No | Pilot lineage and restart state. Retain metadata beyond media lifetime. |
| `working\modular_renderer.sqlite3` | 212 KiB | 12 runs; 50 items, all completed | render diagnostics | No | Playback/recovery/lineage state. Retain until all downstream refs are retired. |

Catalog behavior is partly bounded and partly unbounded:

- `CatalogIndexer` deletes and replaces clip/score rows for a known output ID (`catalog.py:582`, `617`), so repeated re-indexing of the same output does not duplicate those rows.
- New output directories create new `output_runs` and clips. With no output-directory retention, catalog size follows the 3,512 indexed output runs.
- Change events are correctly pruned to seven days/50,000 rows (`catalog.py:19-20`, `405-415`).
- Queue history is intentionally append-only and is stored both in SQLite and monthly checksummed JSONL (`queue_repository.py:263-334`). The compatibility JSON snapshot additionally embeds 5,440 run-history records. This triple representation is useful for rollback but unbounded.
- Historical style recipes/versions are large logical payloads and no current owning style service was found in source; migration/archival should precede pruning.

Important persistent state that must not be mistaken for cache includes queue/control snapshots, queue journals, databases and their sidecars, planner approved manifests, modular production/renderer/pilot lineage, scanner transcript identities, fingerprints, settings overrides/snapshots, product information index, variation profiles/presets, export manifests/status, audit records inside their retention window, and encrypted secrets.

## Duplicate Media Findings

Targeted hashing did not hash every byte blindly. It first grouped all files at least 1 MiB by exact size, then SHA-256 hashed only suspicious groups. That examined 4,051 candidates (9.47 GiB read) and found 297 exact-content groups / 4,047 files with 7,639,925,865 bytes of duplicate excess. A second targeted pass hashed all transcript/checkpoint files, including sub-1 MiB files.

| Duplicate family | Exact duplicate excess | Evidence / safety note |
| --- | ---: | --- |
| Tagged-run `transcript.json` | 5.34 GiB | 5,387 files, 195 distinct contents, 5,733,724,493 excess bytes. Explicit `copy2` on rerun. Canonical content is required; redundant physical copies are not. |
| Tagged-run raw checkpoints | 3.10 GiB | 5,123 files, 196 distinct contents, 3,323,839,909 excess bytes. Same rerun copy path. Keep one canonical recovery artifact per compatible source/config. |
| `assets\broll_intro` vs `assets\product_broll` | 1.39 GiB | 376 files but 244 unique contents. Both paths are actively used for different rendering roles, so do not delete either tree under the current code. Use a content-addressed registry, hardlinks, or role aliases. |
| Electron/build copies | at least 594 MiB | `electron.exe`, DLLs and Chromium resources occur in pnpm layout, flattened `node_modules`, and `dist-desktop`. Rebuildable. |
| Legacy style render/cache/output copies | at least 385 MiB | Same MP4s appear in style run directories, cache, and `trends\automatic_outputs`. Legacy DB references require migration. |
| Trend media across hashtags | at least 55.4 MiB in large-file sample | Same TikTok video ID/content appears in multiple hashtag folders. Current path model is hashtag-scoped. Canonicalize by video ID + SHA-256 with many-to-one hashtag links. |
| Modular outputs | at least 22.5 MiB | Some identical base/render artifacts exist across manual rerenders/pilot paths. Request-key reuse helps active/completed equivalent launches, but manual rerender intentionally creates new output. |

Combined with non-transcript duplicate groups, the project contains at least **11.0 GiB exact duplicate excess**. This is an upper bound on content deduplication, not a deletion recommendation: role paths, DB records, planner manifests, and current jobs must be rewritten or resolved first.

## Cache and Temp Findings

### Confirmed or probable leaks

- **Raw cuts: 1.10 GiB.** Forty completed run folders retain 798,960,227 bytes, three failed folders retain 187,788,195 bytes, one unreferenced async smoke folder retains 59,696,012 bytes, and historical `running` snapshots account for the balance. There are no active current queue entries. Since normal success deletes the raw file, terminal-run raw cuts are a clear cleanup gap, subject to verifying final output/manifest existence.
- **Per-run transcript copies: 8.44 GiB exact duplicate excess.** This is the primary uncontrolled project-local leak.
- **One-off trend analysis: 2.08 GiB.** The completed manifest records 530/530 successful videos and no current code path points at `full_analysis_v2`. The embedded runtime and contact sheets have no TTL.
- **Legacy style cache/output: 781 MiB across three paths.** No current code owner or retention was found.
- **Trend analysis jobs/cache: 123 MiB current + 12.9 MiB validation.** Current code cleans staging/backup directories on transactional success (`trends.py:1383-1429`) but finished job outputs/contact sheets remain unbounded.
- **Preview cache: 5.73 MiB.** Small today but no maximum age/bytes is enforced.
- **Zero-byte artifacts: 623 project files.** Many are old raw cuts or lock files. Zero bytes do not consume much but indicate interrupted/historical state.

### Cleanup that is already working

- FFmpeg/render temporary files are deleted on normal success/failure paths in `main.py` and `ffmpeg_editor.py`.
- Modular renderer deletes each successful composition work directory (`modular_renderer/service.py:263-271`); documentation intentionally retains failed intermediates. No modular `temp`, `.partial`, or staging media remained at audit time.
- Trend downloads use stage/work directories and remove them on success/failure (`trends.py:1378-1429`).
- Root pipeline logs are capped at 25 MiB with four backups; supervisor logs at 10 MiB with three backups; audit logs at 10 MiB with five backups (`logging_utils.py:13-18`). Current logs match those caps.
- Control-job metadata/results have 30-day/2,000-record and 7-day/250-MiB retention and run retention at startup/transitions (`control_services.py:44-49`, `799-907`).
- Change-event retention is bounded.
- Scoring/export uses moves, which avoids creating another normal output copy.

## Completed / Failed / Orphaned Job Storage

### Main queue

- Current queue snapshot: 155 videos — 59 completed, 8 failed, 88 stopped; queue status `stopped`, updated 2026-08-26. **No current active/queued/running job exists.**
- Embedded history: 5,440 runs — 3,862 completed, 1,247 failed, 260 stopped, 69 queued, 2 running. The queued/running values are historical snapshots, not current work.
- 5,477 dated folders are referenced by current JSON history and/or SQLite history; 13 dated folders are unreferenced, totaling 71,814,372 bytes.
- One current failed job points to a missing working folder. Thirteen history records point to missing working folders. This proves that history can survive folder deletion, but deletion must be represented explicitly rather than inferred.
- The unreferenced 59.7 MiB async smoke raw-cut folder is the clearest orphan. It should still be admitted to cleanup through a dry-run/quarantine process, not deleted merely because it is unreferenced.

### Modular systems

- Scanner: 132 completed scans, 3 failed; one completed batch.
- Planner: 11 approved runs and 7 drafts.
- Renderer: 12 completed runs / 50 completed items; all referenced base files exist.
- Variant pilot: 2 completed runs and 1 failed; 6 completed and 4 failed items; 36 output files exist.
- Production: 1 completed, 2 completed-with-failures, 1 failed; 216 variants marked completed.
- Production path integrity: 119 variant rows point to pre-scoring locations that no longer exist, while the files were moved into `export_ready`. Cleanup must resolve manifests/current paths/content identity rather than treating the row path literally.

## Build and Development Storage

| Path | Size | Rebuildability / recommendation |
| --- | ---: | --- |
| `.git` | 2.78 GiB | Not a cache in the ordinary sense. Historical media/log blobs can be removed only with a planned history rewrite, backup, collaborator coordination, and force-push policy. |
| `new_app\node_modules` | 566.32 MiB | Rebuildable from lockfile. pnpm and flattened Electron layouts contain exact duplicate binaries. |
| `new_app\dist-desktop` | 586.96 MiB | Packaged app, unpacked runtime, and two portable installers. Retain latest validated installer and optionally latest rollback version. |
| `new_app\dist` | 584 KiB | Rebuildable Vite output. |
| `working\trends\full_analysis_v2\_runtime` | 834.18 MiB | Completed one-off bundled Python environment; rebuildable from recorded requirements/environment. |
| `working\modular_renderer_seek_benchmark` | 69.06 MiB | Rebuildable benchmark media; keep compact results. |
| `working\test_render_20260720` | 50.73 MiB | Rebuildable test media. |
| `runs` | 34.64 MiB | Training/experiment outputs; retain selected models/metrics, expire routine runs. |
| `__pycache__`, `.pytest_cache` | 11.24 MiB | Rebuildable. |
| `artifacts` | 5.42 MiB | Visual test/review output; apply short retention after sign-off. |
| root/working logs | 107.29 MiB | Bounded already; optional compression and lower backup counts. |

## Existing Cleanup Logic

| Existing mechanism | What it does | Gap |
| --- | --- | --- |
| Raw-cut deletion in `main.py` | Removes raw MP4 after render/cut-only handling | No startup sweep or terminal-job reconciliation for crashes/legacy files |
| FFmpeg temp cleanup | Removes staged `.partial`, stderr/stdout, subtitle and fallback files | Does not own per-run raw directory lifecycle |
| Modular renderer work-dir cleanup | Removes successful item temp dirs | Failed diagnostics have no TTL; completed base outputs have no retention |
| Trend transactional staging cleanup | Removes download stages/backups and restores on failure | Finished analysis jobs, contact sheets and source downloads have no TTL |
| Log rotation | Enforces per-file size and backup count | Logs still use ~107 MiB; compression is optional |
| Control-job retention | Bounds UI job metadata/results | Does not cover pipeline working folders or media |
| Change-event pruning | Seven-day / 50,000-row cap | Other history tables and queue journals remain unbounded |
| Scorer/export moves | Moves clips between tiers/batches and updates manifests | Some newer production DB rows retain stale pre-move paths |
| Request-key reuse in modular services | Reuses equivalent active/completed runs | Manual reruns intentionally retain full duplicate media; no later TTL |

## Storage Leaks / Missing Cleanup

1. **P0 — Physical transcript copies per tagged run.** This is the largest project-local leak and grows with rerun count, not source count.
2. **P0 — No reference-aware terminal-run collector.** Completed/failed/stopped working folders and raw cuts live forever.
3. **P0 — External output retention is unbounded.** `D:\output_clips` is already 2.10 TiB; rejected/review-needed variants are retained alongside export-ready/final media.
4. **P0 — Mutable path references after moves.** Production variant rows can point to old paths after scoring/export moves; this makes naive cleanup unsafe.
5. **P1 — Duplicate role-specific asset trees.** Both paths are active, but 1.39 GiB could be physically deduplicated.
6. **P1 — Base + six variants retained after promotion.** Base files are useful for regeneration, but keeping every base and every rejected variant indefinitely creates a ~3.74× derived-media multiplier.
7. **P1 — One-off analysis environments and contact sheets have no lifecycle.** Completed analysis retains 2.08 GiB.
8. **P1 — Legacy style media/cache has no current owner or migration.** About 781 MiB remains across three paths plus large DB recipe payloads.
9. **P2 — Queue history is represented in JSON snapshot, SQLite and JSONL without compaction.** Small compared with media but monotonically growing.
10. **P2 — Git history contains generated runtime artifacts.** It costs 2.78 GiB locally and every clone, but remediation is operationally risky.

## What Must Never Be Deleted

Without an explicit, verified migration/reference release, never delete:

- Any source VOD or trend media still referenced by an active/resumable job, approved scanner source, planner manifest, production item, or legal/rights retention policy.
- `clipper.sqlite3`, modular SQLite databases, current WAL/SHM sidecars, or queue state/journals while the app may be running.
- Approved planner manifests and composition items used by renderer, pilot, or production lineage.
- Current queue/control state, settings overrides, variation profiles/presets, product information, fingerprints required to validate cache compatibility, export manifests/status, or resume checkpoints for active/resumable jobs.
- Encrypted OAuth/token material under `working\secrets`.
- Final/exported clips still assigned, pending distribution, referenced in exports, or required for audit/compliance.
- A canonical transcript for any retained source or dependent modular scan/variant bridge.
- User-created source assets, documents, fonts, music, SFX, or selected final media merely because another byte-identical path exists.

## What Is Regenerable

Subject to retaining source media, settings/model versions, recipes, and metadata:

- Per-run duplicate transcript/checkpoint copies once a canonical artifact store exists.
- `moments.json` and product detections, though regeneration is computationally expensive and may not be bit-identical if models change.
- Raw cuts and FFmpeg scratch media.
- Preview/proxy media and trend analysis contact sheets/extracted audio.
- Modular base renders from immutable planner manifest + source identity; variants from base/source + profile + transcript bridge.
- Rejected variants when complete edit/recipe/provenance is retained.
- `node_modules`, frontend builds, installers, bytecode/test caches, benchmark/test media, bundled one-off runtimes.
- The catalog’s current projections can often be rebuilt from manifests, but immutable history/user decisions cannot; treat the database as permanent until a supported rebuild/backup process is proven.

## Potentially Reclaimable Storage

| Category / Path | Current Size | Purpose | Required? | Regenerable? | Safe Cleanup Candidate? | Proposed Retention | Evidence |
| --------------- | -----------: | ------- | --------- | ------------ | ----------------------- | ------------------ | -------- |
| Duplicate run transcripts/checkpoints | 9.01 GiB total; 8.44 GiB exact excess | Resume/analysis cache | One canonical copy required | Yes from VOD, but expensive | Only after canonical artifact migration | Canonical per source+fingerprint; run rows reference artifact ID | 195/196 unique contents; `video_queue.py:2277-2281` copies |
| Terminal raw cuts | 1.10 GiB | Render intermediates | No after valid final output; maybe needed for interrupted diagnosis | Yes | Yes after manifest/output verification | Success: immediate; failed/interrupted: 7 days | Normal code deletes them; 799 MiB remains in completed runs |
| `trends\full_analysis_v2` | 2.08 GiB | Completed one-off analysis/runtime | Compact reports/manifests useful | Yes if source trend media retained | Mostly | Keep reports indefinitely; derived frames 30 days after QA; runtime remove after completion | Completed 530/530; no current code references |
| Legacy style media/cache/automatic outputs | 781 MiB | Historical outputs/cache | Selected finals may matter | Likely, from recipes/assets | After migration | Selected finals indefinite; cache 14–30 days / byte cap | Exact duplicate hashes; no current owner source found |
| `new_app\node_modules` | 566 MiB | Build dependencies | Needed only to build/run dev | Yes | Yes when not developing | Reinstall on demand | Lockfile/package manager |
| `new_app\dist-desktop` | 587 MiB | App packages/installers | Latest release/rollback useful | Yes | Old versions yes | Latest 1–2 validated versions | Contains 0.4.0, 0.4.1, unpacked app |
| Test/benchmark/cache/log material | ~220 MiB plus 107 MiB logs | Development/diagnostics | Only within debug window | Yes/diagnostic | Yes with policy | Tests 14–30 days; logs existing cap or compress | Path inventory and log rotation config |
| Duplicate B-roll assets | 1.39 GiB exact excess | Two active roles | Both logical roles required | Source media not assumed regenerable | Not by deleting paths; yes via physical dedup | Content-addressed object + two role refs | 244 unique contents among 376 files; both config roots active |
| Modular base renders | 1.55 GiB | Base playback and variant input | Required while downstream lineage active | Yes | Later | 30–90 days after final promotion and no refs | 50 completed files; DB references exist |
| Modular pilot variants | 526 MiB | Review artifacts | Only until review/promotion/audit horizon | Yes | Later | Failed 7 days; unselected 30 days; selected 90 days or promoted | All runs terminal; 36 outputs |
| Trend analysis jobs/cache | 136 MiB | Derived analysis | Fingerprints/reports useful | Yes | Yes with ref check | Cache 14–30 days / 5–20 GiB cap | Current config root; no global bound |
| Queue snapshots/backups/journals | ~111 MiB plus DB history | Recovery/audit | Current and rollback window required | History recoverable from canonical store | Old redundant copies only | Current + latest backup; compress monthly history; explicit audit horizon | JSON + SQLite + JSONL triple representation |
| `.git` historical generated blobs | 2.78 GiB | Source history | Depends on repository policy | Not necessarily | Only via separate migration | Remove generated paths from future commits; consider history rewrite separately | Pack inspection found raw cuts/log/media |

## Recommended Storage Architecture

### Permanent state

Keep small, backed-up SQLite metadata: source identities, artifact identities, immutable planner manifests, edit recipes, fingerprints, compliance/audit decisions, queue/current job state, export/assignment records, user settings, and lineage. Metadata should refer to immutable `artifact_id` values, not mutable filesystem paths alone.

### Source media

Store original VODs and approved trend/source media under a dedicated source root. Record SHA-256, byte size, mtime, rights/retention class, last reference, and whether a source is reproducibly downloadable. Source deletion must be configurable and blocked while any active/resumable/approved lineage depends on it.

### Final media

Retain only selected/exported/assigned clips and any contractual/audit copy. A clip moved between review/export/batch states should retain the same artifact ID. Prefer one physical object with logical locations/assignments.

### Regenerable media

Represent base clips, variants, previews, contact sheets, extracted audio and raw cuts as derived artifacts with a recipe hash and dependency edges. Metadata outlives the media. When deleted, UI should show “not materialized; can regenerate” rather than losing the record.

### Cache

Use a shared content-addressed store such as `cache\sha256\ab\<hash>` with logical refs. Enforce both TTL and a maximum byte budget, evicting least-recently-used unpinned artifacts whose dependencies and leases permit deletion.

### Temp

Use a run-scoped temp root with an owner/job ID, stage, lease/heartbeat, and creation time. Delete after successful atomic publish; keep failed/interrupted temp for a short diagnostic TTL, then quarantine and purge.

## Recommended Automatic Retention Policies

| Category | Default proposal | Guardrails |
| --- | --- | --- |
| Successful FFmpeg/temp/raw intermediates | Delete immediately after atomic final validation | Keep if final missing/invalid or job active |
| Failed/interrupted temp | 7 days | Extend while retry/resume lease exists |
| Canonical aligned transcript | Retain with source lineage; small and valuable | One physical artifact per source/config fingerprint |
| Raw transcript checkpoint | Retain until aligned transcript + fingerprint validated; then 7–30 days | Keep when alignment retry/resume remains possible |
| Moments/detections | 30–90 days or byte-bounded cache | Pin for active/resumable jobs and reproducibility snapshots |
| Preview/proxy/contact sheets | 14–30 days, LRU, configurable byte cap | Regenerate on demand |
| Rejected/review-needed variants | 14–30 days | Pin when manually reviewed, compliance/audit hold, or planner/export reference exists |
| Unselected modular pilot outputs | 30 days after terminal run | Keep report/profile/lineage indefinitely or longer |
| Modular base media | 30–90 days after all dependent outputs are final/exported | Keep source+recipe; block if any variant job refers to base |
| Final/exported/assigned clips | Indefinite by default or explicit business horizon | Never delete pending delivery/assignment/export refs |
| Original VODs | Configurable 30/90/180 days or archive tier | Only after all derived artifacts are final and regeneration is no longer required, with explicit user policy |
| Trend downloads | 30–90 days after last analysis/link, or pin | Respect rights and approved-media links |
| Logs | Existing size rotation; compress backups; optionally reduce counts | Never delete active log handle; preserve audit logs per policy |
| Queue history | Hot 90 days; compress/archive older months | Preserve checksums and a supported restore/export path |
| Builds/installers | Latest validated + one rollback | Never touch source/lockfiles |
| Git history | No automatic cleanup | Separate repository maintenance only |

## Recommended Clipper Storage UI

Add a Storage page backed by a read-only scanner and an artifact registry. Show:

- Project, source-root, output-root, and free-disk totals separately.
- Permanent state, source media, final media, regenerable media, cache, temp, databases, logs, builds, and exact/likely duplicates.
- Reclaimable bytes split into **safe now**, **after retention**, **after regeneration**, and **blocked by references**.
- Largest paths/files, age distribution, growth over 7/30/90 days, and projected days-to-full.
- Active leases/jobs and why each artifact is protected.
- Broken references: DB path missing, filesystem orphan, manifest path stale, content moved, hash mismatch.
- Controls for dry-run cleanup, cache cap, temp TTL, rejected-variant TTL, source retention, final retention, logs, build versions, and per-artifact pin/unpin.

Every cleanup preview should list the artifact, size, category, retention reason, all references, regeneration recipe availability, and proposed action. “Safe cleanup” must never be a raw folder-age delete.

### Reference-aware cleanup design

1. Introduce an `artifacts` table with immutable ID, SHA-256, size, media type, physical object path, producer/version, created/last-accessed times, materialization state, retention class, `retain_until`, and pin/hold flags.
2. Introduce `artifact_references` edges from jobs, queue runs, planner manifests, render items, production variants, exports, assignments, trend links, and user pins. Store the authoritative artifact ID in each domain row while keeping a transitional path field.
3. Add active leases/heartbeats for running and resumable stages. A candidate with an unexpired lease is never collectible.
4. Reconcile paths and hashes before planning. Moves should update artifact location transactionally; the current stale production paths demonstrate why this is required.
5. Run mark-and-sweep: mark every artifact reachable from active/resumable/permanent records, then evaluate unmarked artifacts against retention policy and regeneration availability.
6. Produce a signed/durable dry-run cleanup plan with exact reasons and byte totals.
7. Recheck references and lease versions immediately before action. Use an application maintenance lock for database/media coordination.
8. Quarantine eligible files by artifact ID for 7–14 days on the same volume, record the move transaction, and support restore. Permanently purge only after another reference check.
9. Delete metadata only after media state is recorded as purged; keep tombstones/checksums so history does not silently point to nowhere.
10. Continuously record category totals and alerts, but keep the scanner read-only unless the user explicitly runs a cleanup plan.

## Proposed Implementation Plan

### P0 — stops uncontrolled disk growth

| Existing module | Current behavior | Proposed behavior | Migration risk | Data-integrity considerations |
| --- | --- | --- | --- | --- |
| `video_queue.py` (`_video_dirs`, `_reuse_base_transcript_for_tagged_run`) and `transcriber.py` | Creates a physical transcript and raw checkpoint copy in every tagged run | Create one canonical transcript artifact keyed by source identity + transcriber/alignment fingerprint; store artifact ID/link in run metadata. Use hardlink/reflink only as a compatibility bridge where supported. | High: many legacy paths and scanner bridge assumptions | Verify source identity, schema, alignment backend and fingerprint; never reuse across changed VOD bytes/settings |
| `main.py` raw-cut lifecycle + queue terminal transitions | Deletes raw files on normal path only | Record raw artifact owner and remove immediately after atomic final validation; startup collector handles expired terminal leftovers | Medium | Confirm manifest/output/hash and job terminal state; preserve interrupted retry leases |
| `clipper_app/modular_production`, `clip_scorer.py`, `export_packager.py` | Files are moved and manifests updated, but production DB output paths can remain stale | Centralize move/publish in artifact service and update all domain references in one transaction/reconciliation event | High | Current 119 stale rows need migration by filename/media ID/hash; no deletion until reconciled |
| output pipeline (`D:\output_clips`) | No global retention; rejected/review/final media accumulate | Add retention class to every output and default TTL for rejected/review media; final/export/pending are pinned | High due 2.10 TiB corpus | Initial mode must be inventory/dry-run only; hash and reconcile manifests/catalog/assignments first |

### P1 — major storage reduction

| Existing module/path | Current behavior | Proposed behavior | Migration risk | Data-integrity considerations |
| --- | --- | --- | --- | --- |
| `assets\broll_intro`, `assets\product_broll`; `variation_engine.py`, `product_broll.py` | Same source files physically copied into two role roots | Shared asset object store with many logical roles/aliases | Medium | Preserve user-facing names and deterministic selection order; validate hashes before dedup |
| modular renderer/pilot/production services | Completed bases and all variants remain indefinitely | Dependency-aware base/variant retention and on-demand rematerialization | Medium-high | Keep planner manifest, source fingerprint, profile revision, transcript bridge version and selected finals |
| `clipper_app/application/trends.py` and analysis roots | Downloads and derived jobs/contact sheets remain | Separate source media from derived analysis cache; LRU/TTL/byte caps | Medium | Never remove approved linked media or a source required to reproduce retained analysis |
| legacy style catalog/path families | Outputs/cache/recipes retained without current owner | Build a one-time migration inventory: selected finals, reproducible recipes, stale cache, tombstones | High because code owner is gone | Preserve user-created recipes/favorites and any selected final; do not infer from age alone |
| queue repository / compatibility snapshot | Full history in JSON, SQLite and JSONL | Keep current state compact; archive/compress immutable history; define one authoritative history store | Medium | Maintain checksum verification and rollback/export tooling |

### P2 — storage visibility and quality of life

| Existing module | Current behavior | Proposed behavior | Migration risk | Data-integrity considerations |
| --- | --- | --- | --- | --- |
| `clipper_app/application/read_services.py`, web API, React app | No unified storage model | Add read-only storage inventory API and Storage page with categories, blockers, trends and dry-run plans | Low | Path containment, bounded scans, no sensitive path/token exposure |
| database status APIs | Counts/integrity exist per subsystem | Add DB size, WAL, freelist, row-growth and last-backup status | Low | Queries read-only; avoid expensive full scans on UI request |
| logging/config | File rotation only | Expose log totals and optional compressed backups | Low | Coordinate with active handlers |
| build/release scripts | Multiple installers/unpacked builds remain | “Retain latest N” build inventory/report command | Low | Never delete source, signing assets, or sole validated release automatically |

### P3 — optional optimization

- Use NTFS hardlinks/reflinks for identical local artifacts when content-addressed storage cannot be introduced immediately.
- Compress old JSON/JSONL history and large recipe payloads while keeping indexed summaries hot.
- Consider archiving cold source/final media to a separate volume/object store with checksum verification.
- Consider a carefully planned Git history rewrite to remove historical generated media/logs. This is separate from runtime storage management.
- Add delta/growth telemetry so the UI can attribute bytes per VOD, base, variant, job, and pipeline stage.

## Largest 100 Files — Raw Inventory Appendix

Format: `bytes | last modified | relative path`.

```text
2184756648 | 2026-08-26T16:28:57 | .git\objects\pack\pack-f0cb7dbb0eb7d53cfb5b439c6d6e9362ad74cf41.pack
613475055 | 2026-08-26T16:28:56 | .git\objects\pack\pack-d9bee28b767810e115cb786417a199cb5b10429c.pack
483729408 | 2026-08-31T13:42:28 | working\catalog\clipper.sqlite3
232351232 | 1980-01-01T08:00:00 | new_app\node_modules\.pnpm\electron@42.5.0\node_modules\electron\dist\electron.exe
232351232 | 2026-08-29T12:30:23 | new_app\dist-desktop\win-unpacked\Clipper.exe
175599795 | 2026-08-31T17:24:04 | .git\objects\pack\pack-dd225d9b7c9bd4609342efe80b3ef04583443a9e.pack
152052017 | 2026-07-10T13:11:26 | new_app\dist-desktop\Clipper-0.4.0-portable.exe
134043648 | 2026-07-17T17:14:07 | working\trends\full_analysis_v2\_runtime\paddle\base\libpaddle.pyd
92649344 | 2026-07-17T17:14:09 | working\trends\full_analysis_v2\_runtime\paddle\libs\mklml.dll
91481716 | 2026-08-29T12:31:56 | new_app\dist-desktop\Clipper-0.4.1-portable.exe
89814528 | 2026-07-17T17:13:50 | working\trends\full_analysis_v2\_runtime\cv2\cv2.pyd
59696012 | 2026-06-03T18:38:48 | working\2026-05-15-10-52-43_async_smoke_20260603_172215\raw_cuts\clip_0001_v2_tight_product_focus_raw.mp4
49452071 | 2026-06-05T11:53:01 | working\2026-05-26-16-05-57_run_087\raw_cuts\clip_0001_v2_tight_product_focus_raw.mp4
48148597 | 2026-06-23T11:44:52 | working\2026-05-20-12-13-59_run_137\raw_cuts\clip_0001_v2_tight_product_focus_raw.mp4
47322112 | 2026-07-17T17:14:08 | working\trends\full_analysis_v2\_runtime\paddle\libs\mkldnn.dll
47008448 | 2026-08-09T07:45:02 | working\2026-07-11-10-23-17_run_201\raw_cuts\clip_0010_v2_transitional_hook_raw.mp4
46923824 | 2026-05-21T15:16:27 | working\2026-05-15-10-52-43_run_025\raw_cuts\clip_0001_v2_tight_product_focus_raw.mp4
44302384 | 2026-05-21T15:16:27 | working\2026-05-15-10-52-43_run_025\raw_cuts\clip_0001_v3_result_overlay_broll_raw.mp4
44302336 | 2026-07-17T17:14:09 | working\trends\full_analysis_v2\_runtime\paddle\libs\phi.dll
42742530 | 2026-07-20T12:57:01 | working\style_renders\276aef2ea8764366b3e7724b96d4b880\styled_draft.mp4
42729520 | 2026-05-21T15:16:27 | working\2026-05-15-10-52-43_run_025\raw_cuts\clip_0001_v0_original_raw.mp4
42467376 | 2026-06-26T06:50:40 | working\2026-05-19-10-27-41_run_144\raw_cuts\clip_0007_v2_tight_product_focus_raw.mp4
40913022 | 2026-07-01T18:34:05 | assets\variation_preview\raw_cut_preview.mp4
38010928 | 2026-05-19T13:25:55 | working\2026-05-15-10-52-43_run_001\raw_cuts\clip_0011_v4_host_focus_fast_broll_raw.mp4
37869227 | 2026-07-20T12:57:29 | working\style_renders\14cb4ef3f5854277b8d1b06d3ca9972a\styled_final.mp4
37480031 | 2026-08-31T11:13:47 | working\modular_renders\c0d0cd7f70bb4f22ab0fa2fff5bf1e86\015_4a0cb3a793914eb2a17093469b77d5c8_serum.mp4
37380110 | 2026-08-31T11:12:48 | working\modular_renders\c0d0cd7f70bb4f22ab0fa2fff5bf1e86\005_29769fcb4a8c4d2c9b6487d6681d0268_serum.mp4
36977941 | 2026-06-05T11:53:01 | working\2026-05-26-16-05-57_run_087\raw_cuts\clip_0001_v3_result_overlay_broll_raw.mp4
36962286 | 2026-08-31T13:24:55 | working\modular_renders\58ae11bdb2d64fc0a53053f37d4f4374\001_403f8d981cce4019935ccff37723bc53_eye_cream.mp4
36950458 | 2026-08-31T11:13:24 | working\modular_renders\c0d0cd7f70bb4f22ab0fa2fff5bf1e86\011_e49dd76fdbb441db9bc64ee37a24e7d9_serum.mp4
36790228 | 2026-08-31T11:13:00 | working\modular_renders\c0d0cd7f70bb4f22ab0fa2fff5bf1e86\007_9371d513030743498c9c0f58cfcbfbac_serum.mp4
36738159 | 2026-07-22T19:00:16 | working\trends\media\downloads\glowingskin\004_7657080317261516053.mp4
36706435 | 2026-05-19T13:26:52 | working\2026-05-15-10-52-43_run_001\raw_cuts\clip_0013_v2_tight_product_focus_raw.mp4
36627009 | 2026-08-29T16:16:34 | working\modular_renders\6a78af694dc3403dbbcf3fda6f3e6eb0\006_6a820d84ccb8433f9e5bdb93b333ad87_serum.mp4
36592968 | 2026-08-29T16:16:10 | working\modular_renders\6a78af694dc3403dbbcf3fda6f3e6eb0\002_5f731a9622804036a3e065f192897315_serum.mp4
36408757 | 2026-08-29T16:16:59 | working\modular_renders\6a78af694dc3403dbbcf3fda6f3e6eb0\010_285de08c256440aaa1e30c8eba278fe3_serum.mp4
36219074 | 2026-06-23T11:44:51 | working\2026-05-20-12-13-59_run_137\raw_cuts\clip_0001_v1_product_broll_open_raw.mp4
36091436 | 2026-08-31T11:13:06 | working\modular_renders\c0d0cd7f70bb4f22ab0fa2fff5bf1e86\008_31a1c4b6282c4be282de1756d3320345_serum.mp4
35571172 | 2026-08-08T17:25:10 | working\2026-05-18-10-39-53_run_201\raw_cuts\clip_0007_v2_transitional_hook_raw.mp4
35551269 | 2026-08-31T11:13:58 | working\modular_renders\c0d0cd7f70bb4f22ab0fa2fff5bf1e86\017_17c795098f344a11856a214889e62ea2_serum.mp4
35547891 | 2026-08-28T11:40:34 | working\modular_renders\0dd6b8da698b45618b465f52b4826c7e\004_e3d124498c3c4cb9811845abbd23c667_serum.mp4
35414537 | 2026-08-28T11:39:25 | working\modular_renders\0dd6b8da698b45618b465f52b4826c7e\003_a2bfaffdcbdf40f68cfa98e74ed5fc71_serum.mp4
35351191 | 2026-08-28T12:49:02 | working\modular_renders\d0dbaec03f9a4e38a87359b59883ef66\003_a2bfaffdcbdf40f68cfa98e74ed5fc71_serum.mp4
35243121 | 2026-08-29T16:16:22 | working\modular_renders\6a78af694dc3403dbbcf3fda6f3e6eb0\004_947e5a650bd941b0907f452ceb22c39c_serum.mp4
35153110 | 2026-06-05T11:53:00 | working\2026-05-26-16-05-57_run_087\raw_cuts\clip_0001_v0_original_raw.mp4
34977657 | 2026-08-29T16:16:28 | working\modular_renders\6a78af694dc3403dbbcf3fda6f3e6eb0\005_8ae74a4ce4bb4d53828906c2fbdaa5b4_serum.mp4
34910594 | 2026-08-31T11:12:31 | working\modular_renders\c0d0cd7f70bb4f22ab0fa2fff5bf1e86\002_db518bfa4ece44589e32555e3e577099_serum.mp4
34831930 | 2026-08-31T13:24:36 | working\modular_renders\528fbd145e914c8fbf3242debcd8994a\001_13c1e8e75abe404eb9fb629f22378ba5_cleanser.mp4
34789740 | 2026-08-28T11:52:31 | working\modular_renders\30ae9ef29f5b42ea9fac9918e37eb709\005_d22bf435df194237b984ad81e4dd2c89_cleanser.mp4
34597180 | 2026-05-19T13:03:23 | working\2026-05-16-14-16-00_run_011\raw_cuts\clip_0001_v1_cream_soft_bold_yellow_broll_raw.mp4
34551014 | 2026-06-23T11:44:49 | working\2026-05-20-12-13-59_run_137\raw_cuts\clip_0001_v0_original_raw.mp4
34510796 | 2026-08-31T11:12:42 | working\modular_renders\c0d0cd7f70bb4f22ab0fa2fff5bf1e86\004_8714ac42e1884ac3b87c304f5a9b7102_serum.mp4
34353428 | 2026-08-28T11:51:40 | working\modular_renders\30ae9ef29f5b42ea9fac9918e37eb709\001_0f56db817356451d8baddba5a0faf3db_cleanser.mp4
34320305 | 2026-08-28T12:49:08 | working\modular_renders\73895d64fbaf4f6eb895293f14eac57a\001_0f56db817356451d8baddba5a0faf3db_cleanser.mp4
34289524 | 2026-08-28T11:52:01 | working\modular_renders\30ae9ef29f5b42ea9fac9918e37eb709\003_ed3878063b9947f197bf1489fc41b118_cleanser.mp4
34250693 | 2026-08-31T13:24:48 | working\modular_renders\fef05013945642c3a6c1422d1d6b0265\001_93130adf28094c9cb232fe550cd71ff8_serum.mp4
34009978 | 2026-08-28T11:41:40 | working\modular_renders\0dd6b8da698b45618b465f52b4826c7e\005_f5abb89cb8e9423ca297e85c6a5a89ec_serum.mp4
33923128 | 2026-08-31T13:24:42 | working\modular_renders\2233db125cc943e1a95c3dbcf5aac9a5\001_3d4f1093afc940ff9ee8dc323ac9e8c7_toner.mp4
33552169 | 2026-08-31T11:13:52 | working\modular_renders\c0d0cd7f70bb4f22ab0fa2fff5bf1e86\016_0ce8c8d333ee45889209d4b451b8e179_serum.mp4
33501523 | 2026-08-31T11:12:54 | working\modular_renders\c0d0cd7f70bb4f22ab0fa2fff5bf1e86\006_ee3f30d240104e2387a298668dfe63e5_serum.mp4
32825907 | 2026-08-31T11:14:14 | working\modular_renders\c0d0cd7f70bb4f22ab0fa2fff5bf1e86\020_8afd63fd3416487b9bb21533e951cf4e_serum.mp4
32646373 | 2026-06-03T18:56:33 | working\2026-05-23-10-46-48_run_080\raw_cuts\clip_0001_v5_clean_commerce_raw.mp4
32394256 | 2026-08-31T11:13:41 | working\modular_renders\c0d0cd7f70bb4f22ab0fa2fff5bf1e86\014_25ed78ab439d4433beb492ac2578a9e7_serum.mp4
32393358 | 2026-08-29T16:16:40 | working\modular_renders\6a78af694dc3403dbbcf3fda6f3e6eb0\007_200b5a185c9e4f7fb114188de241fb4e_serum.mp4
32365427 | 2026-08-31T11:13:29 | working\modular_renders\c0d0cd7f70bb4f22ab0fa2fff5bf1e86\012_21fb2e21935a416191220ade5c72a652_serum.mp4
32329616 | 2026-08-28T11:37:44 | working\modular_renders\0dd6b8da698b45618b465f52b4826c7e\001_15d9013eba4744a8ada3d46e5d7a6bc9_serum.mp4
32290826 | 2026-08-08T16:39:08 | working\2026-05-18-10-39-53_run_201\raw_cuts\clip_0012_v0_original_raw.mp4
32243760 | 2026-06-26T06:50:40 | working\2026-05-19-10-27-41_run_144\raw_cuts\clip_0007_v1_product_broll_open_raw.mp4
32070507 | 2026-08-28T12:48:55 | working\modular_renders\d0dbaec03f9a4e38a87359b59883ef66\001_15d9013eba4744a8ada3d46e5d7a6bc9_serum.mp4
32068789 | 2026-08-31T13:25:07 | working\modular_renders\bbe0123fc7c94676846cc906e252b7a4\001_294ea5bbbef840d69776ae3cd544bbee_skin_cream.mp4
31852027 | 2026-08-31T11:13:35 | working\modular_renders\c0d0cd7f70bb4f22ab0fa2fff5bf1e86\013_96a805f8404548cba455defa5b85d322_serum.mp4
31792538 | 2026-08-28T11:38:41 | working\modular_renders\0dd6b8da698b45618b465f52b4826c7e\002_d2580a5e7c4d4bb2bce25e1b392f34fc_serum.mp4
31702276 | 2026-08-29T16:16:46 | working\modular_renders\6a78af694dc3403dbbcf3fda6f3e6eb0\008_d569337ab323466ebb36f7e2fc22d0c3_serum.mp4
31407674 | 2026-08-29T16:16:03 | working\modular_renders\6a78af694dc3403dbbcf3fda6f3e6eb0\001_3328b117540b40ef90f7e14cfb789b3f_serum.mp4
31392462 | 2026-08-31T11:12:25 | working\modular_renders\c0d0cd7f70bb4f22ab0fa2fff5bf1e86\001_78f2b06863b440ba8f38e54987e1c326_serum.mp4
31303736 | 2026-08-29T16:16:16 | working\modular_renders\6a78af694dc3403dbbcf3fda6f3e6eb0\003_8fc26de507bb42cdac017bbd12e140eb_serum.mp4
31241571 | 2026-08-29T16:16:52 | working\modular_renders\6a78af694dc3403dbbcf3fda6f3e6eb0\009_35b3c88ebcea46e9b534420576f04c5a_serum.mp4
30583419 | 2026-08-28T11:51:52 | working\modular_renders\30ae9ef29f5b42ea9fac9918e37eb709\002_e74951ab3fbd4273b43997e40e6ccca7_cleanser.mp4
30408752 | 2026-06-26T06:50:40 | working\2026-05-19-10-27-41_run_144\raw_cuts\clip_0007_v0_original_raw.mp4
29895898 | 2026-08-31T11:13:18 | working\modular_renders\c0d0cd7f70bb4f22ab0fa2fff5bf1e86\010_297d7e3ec7a744a0a3028b4eca7960e9_serum.mp4
29814952 | 2026-06-03T18:56:46 | working\2026-05-23-10-46-48_run_080\raw_cuts\clip_0002_v1_product_broll_open_raw.mp4
29724501 | 2026-08-31T13:25:01 | working\modular_renders\4c0c3104ac0b4ea899221e961193abc5\001_feb349c959b043e189347a6468f2910d_mask.mp4
29449186 | 2026-07-22T19:06:02 | working\trends\media\downloads\lipstick\003_7657798820285254926.mp4
29406354 | 2026-08-28T12:49:13 | working\modular_renders\73895d64fbaf4f6eb895293f14eac57a\004_4695038078a24c09a0cd96a663286204_cleanser.mp4
29311425 | 2026-08-31T11:12:36 | working\modular_renders\c0d0cd7f70bb4f22ab0fa2fff5bf1e86\003_96ed10ece33d423bbea6f56aef3ea32f_serum.mp4
29100443 | 2026-08-28T11:52:08 | working\modular_renders\30ae9ef29f5b42ea9fac9918e37eb709\004_4695038078a24c09a0cd96a663286204_cleanser.mp4
28623102 | 2026-08-31T11:14:09 | working\modular_renders\c0d0cd7f70bb4f22ab0fa2fff5bf1e86\019_7734dfe8c42b4ab4b8ea5a10d0ea8c1a_serum.mp4
28569273 | 2026-08-08T17:29:16 | working\2026-05-18-10-39-53_run_201\raw_cuts\clip_0008_v4_b_roll_only_raw.mp4
28448894 | 2026-06-03T18:56:37 | working\2026-05-23-10-46-48_run_080\raw_cuts\clip_0002_v0_original_raw.mp4
28148848 | 2026-08-26T12:05:48 | working\video_queue_state.json
27797105 | 2026-06-16T00:06:19 | working\2026-06-03-11-52-23_run_106\raw_cuts\clip_0012_v2_tight_product_focus_raw.mp4
27568890 | 2026-08-01T12:22:36 | working\trends\media\downloads\emina\004_7667007663330348309.mp4
27327646 | 2026-08-31T11:13:12 | working\modular_renders\c0d0cd7f70bb4f22ab0fa2fff5bf1e86\009_39160ef94ad54365a9b4bbf942d72d7e_serum.mp4
26532084 | 2026-08-25T09:12:15 | working\2026-06-09-11-45-54_run_212\raw_cuts\clip_0003_v5_transitional_bb_raw.mp4
26450858 | 2026-08-31T11:14:04 | working\modular_renders\c0d0cd7f70bb4f22ab0fa2fff5bf1e86\018_182a7c97f01f41f098636b6e7e4c5f69_serum.mp4
26391552 | 2026-07-17T17:13:50 | working\trends\full_analysis_v2\_runtime\cv2\opencv_videoio_ffmpeg4100_64.dll
26214340 | 2026-07-11T16:13:41 | pipeline.log.3
26214313 | 2026-08-26T01:51:10 | pipeline.log.1
26213232 | 2026-07-26T17:47:53 | pipeline.log.2
26207933 | 2026-07-20T14:05:20 | working\test_render_20260720\PROYA_VitaminC_Trend_Test_Final.mp4
```

## DO NOT IMPLEMENT

This report is analysis only. No cleanup, retention migration, database maintenance, code change, media mutation, or storage-management feature was implemented.

AUDIT COMPLETE — NO FILES DELETED AND NO STORAGE BEHAVIOR CHANGED.
