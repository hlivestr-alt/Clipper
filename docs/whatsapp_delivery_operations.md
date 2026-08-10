# WhatsApp Delivery Operations

Clipper can build a permanent WhatsApp-compatible media mirror, register export batches, assign them idempotently to affiliates, track per-item send outcomes, and project assignments to Google Sheets through an outbox. Direct assignment/send state is fail-closed by default.

## Canonical Storage and State

Current configured paths are:

- Permanent delivery media: `D:\output_clips\export_batches_whatsapp\<batch-number>`.
- Authoritative SQLite state: `D:\output_clips\export_batches_whatsapp\_whatsapp_state.sqlite3`.
- Affiliate assignment views/links: `D:\output_clips\affiliate_assignments`.

Numeric batch folders are immutable delivery inventory. Do not rename, move, merge, or edit them after publication. Affiliate names, ownership, status, per-item WhatsApp IDs, events, outbox rows, and the packaging floor belong in SQLite, not in folder names.

`export_packager.py` integrates with this state when `WHATSAPP_DELIVERY_ENABLED=True`: it enforces the diversity-first rolling strategy, respects the authoritative packaging floor, and registers newly published batches. The current direct-delivery flags do not have to be enabled for media creation/registration.

Historical sources and folders previously renamed by external workflows are read-only. Use a reviewed source ledger when their current paths no longer match the numeric batch.

## Media Policy

`whatsapp_media.py` probes and classifies media for copy, remux, transcode, or review. The configured policy targets H.264/AAC MP4, at most 30 FPS, conservative 16,000,000-byte maximum and 15,000,000-byte encode target, GOP/level limits, BT.709 output, full decode/validation options, and explicit handling for HDR, unknown color, and approved stale-nclx cohorts.

Keep the policy revision recorded with conversion runs. Do not resume a stopped run under a changed policy unless `--migrate-run-policy` is an intentional, reviewed action.

## Backlog Conversion

The default command is a read-only probe inventory:

```powershell
python scripts/whatsapp_backlog.py
```

Media creation requires `--execute`. Resume continues an existing run and never creates a replacement run:

```powershell
python scripts/whatsapp_backlog.py --execute --batch-range 6901:6905
python scripts/whatsapp_backlog.py --execute --resume
python scripts/whatsapp_backlog.py --execute --resume-run <run-id>
```

Use one encode/probe worker for the first pilot. Destination files are staged below `_tmp\<run-id>\<batch-number>`, fully validated, then the entire batch directory is published atomically. `--adopt-existing`, `--copy-forward-pending`, `--bootstrap-future-packaging`, policy migration, and run abandonment are administrative recovery tools; inspect `--help` and review state before using them.

## Authenticated API and CLI

All WhatsApp delivery API routes are sensitive and require Clipper's Bearer token. The administration CLI reads `CLIPPER_CONTROL_TOKEN` and defaults to `CLIPPER_API_URL` or `http://127.0.0.1:8765`:

```powershell
$env:CLIPPER_CONTROL_TOKEN = '<same token as the running backend>'
python scripts/whatsapp_delivery.py status
python scripts/whatsapp_delivery.py claim --affiliate-name "Affiliate A" --affiliate-identifier affiliate-a --idempotency-key claim-001
```

The CLI also exposes `start`, `sent`, `fail`, `cancel`, `release`, and `retry`, each requiring the assignment ID, expected version, and an idempotency key.

## n8n Contract

SQLite is authoritative. n8n must use the authenticated HTTP API and must never open the database directly.

1. `POST /api/whatsapp-delivery/claims` with stable affiliate identity and idempotency key.
2. Use the returned canonical path and immutable file manifest read-only.
3. `POST /api/whatsapp-delivery/assignments/{id}/start` with the expected assignment version.
4. After each upload/send, `PUT /api/whatsapp-delivery/assignments/{id}/items` with the relative path, status, and WhatsApp media/message IDs.
5. Call `.../sent` only after every item is confirmed sent.
6. On failure, call `.../fail`; retry the same assignment rather than claiming a replacement.
7. Do not automatically retry an `outcome_uncertain` item.

Concurrent claims are serialized with `BEGIN IMMEDIATE`. Reusing an idempotency key returns the same logical assignment/transition rather than duplicating ownership.

## Delivery Cutover Gate

Claims and assignment/send-state mutations are blocked unless both evaluated settings are true:

```text
WHATSAPP_DIRECT_PC_DELIVERY_ENABLED=True
WHATSAPP_LEGACY_DRIVE_WORKFLOW_DISABLED=True
```

Both defaults are `False`. They are privileged settings and should be changed in operator-managed configuration, not casually through the browser.

Cut over in this order:

1. Stop legacy assignment/send triggers that can compete for the same affiliates or inventory.
2. Verify Drive automation cannot rename/move/delete/write the local mirror, write Clipper SQLite, or claim/send the same assignment.
3. Set `WHATSAPP_LEGACY_DRIVE_WORKFLOW_DISABLED=True` as the operator attestation.
4. Set `WHATSAPP_DIRECT_PC_DELIVERY_ENABLED=True`.
5. Restart Clipper and verify `/api/whatsapp-delivery/status` reports `claims_enabled=true`.
6. Perform one idempotent claim/start/item/sent pilot on Android and iPhone before enabling schedules.

Drive-side renaming may remain only when it is completely isolated from local canonical media, SQLite, affiliate ownership, and the new direct workflow.

## Google Sheets Projection

Google Sheets is a projection and affiliate roster, never the ownership authority. n8n reads `GET /api/whatsapp-delivery/sheet-outbox`, upserts using `affiliate_assignment_id`, then acknowledges with `POST /api/whatsapp-delivery/sheet-outbox/{outbox_id}/ack`. Failed Sheet updates remain retryable and do not roll back the SQLite assignment.

## Full-Rollout Prerequisites

- Approve the historical source ledger and packaging-floor bootstrap.
- Confirm whether n8n can read the local `D:` path; otherwise provide an authenticated media-download or approved upload bridge.
- Configure WhatsApp credentials, webhooks, recipients, and test devices outside this repository.
- Complete copy/remux/transcode, color, frame-rate, size, concurrent-claim, idempotency, API delivery, Sheet outbox, and device-playback pilots.
- Keep backups of the SQLite state and do not modify canonical batch media after registration.
