# Modular Scanner v1

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

Analyzer and prompt semantics are versioned as `modscan-v2` and `modscan-prompt-v2`, so
normal Scan does not reuse v1 analysis. Existing generations remain readable and are not
deleted during the scanner schema's additive diagnostics migration.
