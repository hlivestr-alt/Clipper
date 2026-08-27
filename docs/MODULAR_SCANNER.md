# Modular Scanner v3.2

The Modular Scanner is a standalone `/modules` workspace. It stores source ranges in
`WORKING_DIR/modular_library.sqlite3` and scanner-owned transcript copies under
`WORKING_DIR/modular_scanner/transcripts/`. It does not invoke the production pipeline,
create clips, render media, or write the main Clipper catalog.

## Production coordination

Before fresh transcription and before every LM Studio request, the scanner reads the
existing queue, control, and supervisor JSON snapshots. Active or queued production
causes the scan to persist `waiting_for_production` and poll until those snapshots are
idle. An LM Studio request already in flight is allowed to finish and is checkpointed
before the scanner waits.

This is intentionally cooperative. The status snapshots are the narrowest existing
read-only source and can be stale if an external production process stops updating its
files. v1 does not add heartbeats, hard cancellation, GPU claims, or production
instrumentation.

## Results and generations

`Scan VOD` reuses a completed result only when source identity, transcript fingerprint,
analyzer version, prompt version, and exact `LM_STUDIO_MOMENT_MODEL_ID` all match.
`Rescan` creates an immutable generation; the previous successful generation remains
current until the new generation completes successfully.

`Scan All Unscanned VODs` is a batch-orchestration layer over that same normal Scan
path. Its preview uses directory entries, file size/mtime, and already-persisted scanner
metadata only. Known sources are estimated as compatible/current, already active, or
would queue; new or changed files are reported as needing evaluation. Preview never
hashes a VOD, runs FFprobe, or reads transcript content.

Launch persists a `preparing` batch and returns HTTP 202 immediately. The existing single
scanner worker then discovers each source in deterministic order, performs authoritative
fingerprint, duration, transcript, and compatibility checks, and calls the normal Scan
creation path. A second launch while a batch is preparing or running returns that active
batch instead of creating another. The preview is only a confirmation estimate; worker
preparation is authoritative.

The confirmation UI does not launch a zero-work batch. While a launched batch runs, its
compact status panel shows total, skipped, queued, completed, failed, remaining, and the
currently executing filename without exposing filesystem paths. The existing single
scanner worker remains the only executor, including its `waiting_for_production`
transitions. One failed generation is recorded normally and does not stop later queued
generations.

Batch membership is persisted in the existing `modular_library.sqlite3` through the
additive `scan_batches`, `scan_batch_items`, and bounded preparation-failure rows (schema
version 4). Batch state and the discovered count are written separately; every item is
committed in a short transaction after its filesystem work finishes. Items only reference
source and ordinary scan IDs plus their disposition; live scan execution state is not
duplicated. Preparing batches and ordinary incomplete scans are re-enqueued on restart,
and already-recorded paths are skipped. Status is reconstructed from persisted batch/item
and scan rows.

If canonical path, size, and mtime match an existing source row, preparation reuses its
content fingerprint and duration. New or changed files are hashed and probed in the worker,
outside SQLite write transactions. Source revalidation likewise accepts unchanged metadata
and rejects size/mtime changes, avoiding a duplicate hash immediately before normal Scan
creation.

The authenticated batch API consists of:

- `GET /api/modular-scanner/batch-scan-preview`
- `POST /api/modular-scanner/batches`
- `GET /api/modular-scanner/batches/{batch_id}`

There is deliberately no batch rescan, retry, cancellation, deletion, scheduler, or
parallel execution API.

Long transcripts are split on authoritative segment boundaries. Every initial window
must satisfy both the character/token safety budget and a 15-minute VOD-time cap.
Windows overlap, keep absolute VOD timestamps, and use deterministic midpoint ownership
in overlaps.

An empty analyzer response is checked against deterministic transcript product anchors.
A product-rich empty window is retried as roughly 7-8 minute authoritative-boundary
children, with at most one further subdivision; product-empty windows remain valid empty
results. Recovery stays inside the same scan generation and the parent checkpoint stores
the recovered candidates.

Stored text is reconstructed from the authoritative timed transcript after strict raw
product/role enum, bounds, duration, coverage, and product-evidence validation. Transcript
evidence matching is separately normalized for attached Indonesian `-nya` forms and a
narrow scanner-only set of Whisper aliases such as `air krim` and `sheet mesh`; those
forms never become valid analyzer product enum values. When direct range evidence is
absent, the validator may use the closest consistent evidence within 30 seconds, while
still rejecting conflicting products.

The hard duration minimum remains 15.000 seconds. A snapped 10.000-14.999 second
candidate may expand to the smallest coherent adjacent authoritative boundary when the
same product discussion remains active. Conflicting products and unrelated filler cannot
be used as padding. Repair outcomes are stored in scanner-owned validation diagnostics.

Before short candidates are rejected, v3 also considers adjacent composition. It joins
at most three candidates with the exact same product and role, in natural timestamp order,
with no more than 8 seconds between neighbors. A product transition, unrelated filler,
missing authoritative coverage, or a cross-product/cross-role neighbor stops the chain.
The first chain reaching 15.000 seconds wins; a shorter valid authoritative-boundary
repair wins instead when available. Stored transcript text is always reconstructed across
the complete authoritative range.

v3.1 treats a transcript-free pause inside the configured 8-second gap as silence, not
as an interruption. Authoritative coverage is checked across the complete composed range;
explicit conflicting product speech or unrelated filler inside the gap still blocks the
merge. This preserves conservative composition while allowing adjacent price and checkout
statements separated by a short pause.

Composed confidence is the source-duration-weighted average minus `0.005 * total positive
gap seconds`, clamped to `[0, 1]`. Segment diagnostics store source ordinals, ranges,
durations, confidences, the formula, and the final result. Each consumed raw candidate
also receives an additive `composed_into_segment` diagnostic row and is not counted as a
`duration_too_short` rejection. Composition then enters the normal deterministic deduper.

v3 derives a forward active-product timeline from authoritative transcript segments.
Unambiguous explicit product evidence establishes or refreshes context for up to 120
seconds; another explicit product, ambiguous multi-product speech, recognized unrelated
topic filler, or a safely mapped etalase transition ends or replaces it. Explicit evidence
inside a candidate always wins. Validation priority is local explicit evidence, explicit
active context, bounded nearby evidence, then safe etalase-derived active context.

In v3.1, inherited context is primarily positive support. An ordinary inherited mismatch
does not independently establish `conflicting_product`; without other support the candidate
remains `product_not_supported`. Hard context conflicts require a confirmed explicit or
safely inferred etalase transition within the 30-second nearby interval. Direct local
matching evidence always overrides stale inherited context, while direct local contradictory
or multi-product evidence still rejects. The guarded etalase parser also recognizes the
observed `telasan` ASR form, but only numbers already confirmed by an exact explicit
number/product pairing can establish context.

v3.2 preserves normalized candidates from overlapping analysis windows until a
cross-window product check runs, before midpoint ownership can discard either label. The
check applies only to different chunks, identical roles, different declared products, and
a temporal IoU of at least 0.50. Same-product duplicates and incidental lower-overlap
neighbors continue through normal ownership and deduplication.

For a detected disagreement, deterministic resolution checks explicit product evidence
inside the overlapping source range, then a confirmed explicit transition within 30
seconds, fresh explicitly established active context within 30 seconds, and finally the
existing safely confirmed etalase context. LLM confidence, chunk order, ownership, and
duration never decide product identity. A supported product wins even when its original
window did not own the midpoint; without a unique deterministic winner, every candidate
in the conflicting region is rejected with `cross_window_product_conflict`.

Composition and boundary repair still run per window under the v3.1 rules before this
check, so composed candidates participate like ordinary candidates. Conflict provenance
uses the existing bounded scanner diagnostic rows and segment diagnostics; no schema
migration is required.

The scanner-specific PROYA mapping is Etalase 1 Cleanser, 2 Toner, 3 Serum, 4 Skin Cream,
5 Eye Cream, and 6 Mask. An etalase number becomes usable for inference only when that
exact number/product pair is confirmed by explicit product evidence in the same transcript
segment. Contradictory explicit evidence wins and does not confirm the mapping.

Analyzer and prompt semantics remain versioned as `modscan-v3.2` and
`modscan-prompt-v3`, so normal Scan does not reuse v1/v2/v3/v3.1 analysis. The batch
feature does not change or bump either semantic version. Composition and context
provenance still use the existing diagnostics JSON and additive diagnostic rows; schema
version 4 only extends the lightweight batch orchestration persistence described above. Existing
v1/v2/v3/v3.1 generations remain readable and are never rewritten or deleted.
