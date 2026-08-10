from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from clipper_app.contracts.whatsapp_delivery_models import WhatsAppClaimRequest


DELIVERY_SCHEMA_VERSION = 1
MEDIA_STATES = {"pending", "processing", "incomplete", "conflict", "complete"}
DELIVERY_STATES = {
    "unassigned",
    "assigned",
    "sending",
    "sent",
    "delivery_failed",
    "cancelled",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class WhatsAppDeliveryError(RuntimeError):
    pass


class WhatsAppNotFound(WhatsAppDeliveryError):
    pass


class WhatsAppConflict(WhatsAppDeliveryError):
    pass


class WhatsAppDeliveryService:
    """Authoritative media-readiness and affiliate assignment store."""

    def __init__(
        self,
        database_path: str | Path,
        mirror_root: str | Path,
        *,
        timeout: float = 10.0,
        direct_pc_delivery_enabled: bool = True,
        legacy_drive_workflow_disabled: bool = True,
    ) -> None:
        self.database_path = Path(database_path).expanduser().resolve()
        self.mirror_root = Path(mirror_root).expanduser().resolve()
        self.timeout = max(1.0, float(timeout))
        self.direct_pc_delivery_enabled = bool(direct_pc_delivery_enabled)
        self.legacy_drive_workflow_disabled = bool(legacy_drive_workflow_disabled)
        self._schema_lock = threading.Lock()
        self._schema_ready = False

    @classmethod
    def from_config(cls, cfg: Any) -> "WhatsAppDeliveryService":
        output_root = Path(str(getattr(cfg, "OUTPUT_DIR", "D:/output_clips")))
        mirror_root = output_root / str(
            getattr(cfg, "EXPORT_BATCH_DIR_NAME", "export_batches_whatsapp")
        )
        database_path = Path(
            str(
                getattr(
                    cfg,
                    "WHATSAPP_STATE_DB",
                    mirror_root / "_whatsapp_state.sqlite3",
                )
            )
        )
        return cls(
            database_path,
            mirror_root,
            direct_pc_delivery_enabled=bool(
                getattr(cfg, "WHATSAPP_DIRECT_PC_DELIVERY_ENABLED", False)
            ),
            legacy_drive_workflow_disabled=bool(
                getattr(cfg, "WHATSAPP_LEGACY_DRIVE_WORKFLOW_DISABLED", False)
            ),
        )

    @property
    def direct_claims_enabled(self) -> bool:
        return self.direct_pc_delivery_enabled and self.legacy_drive_workflow_disabled

    def _require_direct_delivery_cutover(self) -> None:
        if self.direct_claims_enabled:
            return
        if not self.direct_pc_delivery_enabled:
            reason = "WHATSAPP_DIRECT_PC_DELIVERY_ENABLED is false"
        else:
            reason = (
                "the legacy Google Drive assignment/delivery workflow has not "
                "been explicitly confirmed disabled"
            )
        raise WhatsAppConflict("Direct PC-to-WhatsApp delivery is blocked: " + reason)

    def _connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.database_path,
            timeout=self.timeout,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute(f"PRAGMA busy_timeout={int(self.timeout * 1000)}")
        return connection

    def ensure_schema(self) -> None:
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            connection = self._connect()
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.executescript(_SCHEMA)
                connection.execute(
                    "INSERT OR IGNORE INTO whatsapp_meta(key,value) VALUES('schema_version',?)",
                    (str(DELIVERY_SCHEMA_VERSION),),
                )
                connection.commit()
            finally:
                connection.close()
            self._schema_ready = True

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        self.ensure_schema()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()

    def register_media_batch(
        self,
        batch_number: int,
        canonical_path: str | Path,
        files: list[dict[str, Any]],
        *,
        media_state: str = "complete",
        ready_for_delivery: bool = True,
        audit_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        if media_state not in MEDIA_STATES:
            raise ValueError(f"Unknown media state: {media_state}")
        path = Path(canonical_path).resolve()
        expected = (self.mirror_root / str(int(batch_number))).resolve()
        if path != expected:
            raise WhatsAppConflict(
                f"Canonical batch path must be {expected}; received {path}"
            )
        if ready_for_delivery and media_state != "complete":
            raise WhatsAppConflict("Only complete media can be ready for delivery")
        fingerprint = audit_fingerprint or hashlib.sha256(
            json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        now = _utc_now()
        with self.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT * FROM media_batches WHERE batch_number=?", (int(batch_number),)
            ).fetchone()
            if existing and str(existing["canonical_path"]) != str(path):
                raise WhatsAppConflict("Batch number already owns another canonical path")
            if existing and existing["current_assignment_id"] and not ready_for_delivery:
                # Revoking readiness is allowed; assignment is preserved for audit.
                pass
            connection.execute(
                """
                INSERT INTO media_batches(
                    batch_number,canonical_path,media_state,ready_for_delivery,
                    expected_file_count,published_at,audit_fingerprint,
                    affiliate_delivery_state,version,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,0,?)
                ON CONFLICT(batch_number) DO UPDATE SET
                    media_state=excluded.media_state,
                    ready_for_delivery=excluded.ready_for_delivery,
                    expected_file_count=excluded.expected_file_count,
                    audit_fingerprint=excluded.audit_fingerprint,
                    version=media_batches.version+1,
                    updated_at=excluded.updated_at
                """,
                (
                    int(batch_number),
                    str(path),
                    media_state,
                    int(ready_for_delivery),
                    len(files),
                    now,
                    fingerprint,
                    "unassigned",
                    now,
                ),
            )
            connection.execute(
                "DELETE FROM media_files WHERE batch_number=?", (int(batch_number),)
            )
            for ordinal, item in enumerate(files):
                connection.execute(
                    """
                    INSERT INTO media_files(
                        batch_number,relative_path,ordinal,size_bytes,fingerprint,
                        compliance_json,validated_at
                    ) VALUES(?,?,?,?,?,?,?)
                    """,
                    (
                        int(batch_number),
                        str(item["relative_path"]),
                        ordinal,
                        int(item.get("size_bytes") or 0),
                        str(item.get("fingerprint") or ""),
                        json.dumps(item.get("compliance") or {}, separators=(",", ":")),
                        now,
                    ),
                )
            self._event(
                connection,
                batch_number,
                None,
                "media_batch_registered",
                {"ready_for_delivery": ready_for_delivery, "media_state": media_state},
            )
        return self.batch(batch_number)

    def mark_media_state(
        self,
        batch_number: int,
        canonical_path: str | Path,
        *,
        media_state: str,
        expected_file_count: int,
        reason: str,
    ) -> dict[str, Any]:
        if media_state not in {"pending", "processing", "incomplete", "conflict"}:
            raise ValueError("mark_media_state requires a non-complete media state")
        path = Path(canonical_path).resolve()
        expected = (self.mirror_root / str(int(batch_number))).resolve()
        if path != expected:
            raise WhatsAppConflict(
                f"Canonical batch path must be {expected}; received {path}"
            )
        now = _utc_now()
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO media_batches(
                    batch_number,canonical_path,media_state,ready_for_delivery,
                    expected_file_count,published_at,audit_fingerprint,
                    affiliate_delivery_state,version,updated_at
                ) VALUES(?,?,?,0,?,NULL,?,'unassigned',0,?)
                ON CONFLICT(batch_number) DO UPDATE SET
                    media_state=excluded.media_state,
                    ready_for_delivery=0,
                    expected_file_count=excluded.expected_file_count,
                    audit_fingerprint=excluded.audit_fingerprint,
                    version=media_batches.version+1,
                    updated_at=excluded.updated_at
                """,
                (
                    int(batch_number),
                    str(path),
                    media_state,
                    max(0, int(expected_file_count)),
                    hashlib.sha256(reason.encode()).hexdigest(),
                    now,
                ),
            )
            self._event(
                connection,
                int(batch_number),
                None,
                f"media_{media_state}",
                {"reason": reason},
            )
        return self.batch(batch_number)

    def batch(self, batch_number: int) -> dict[str, Any]:
        self.ensure_schema()
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM media_batches WHERE batch_number=?", (int(batch_number),)
            ).fetchone()
            if not row:
                raise WhatsAppNotFound(f"Batch {batch_number} was not found")
            return self._batch_payload(connection, row)
        finally:
            connection.close()

    def claim(self, request: WhatsAppClaimRequest, *, actor: str) -> dict[str, Any]:
        self._require_direct_delivery_cutover()
        now = _utc_now()
        with self.transaction(immediate=True) as connection:
            existing_key = connection.execute(
                "SELECT assignment_id,request_fingerprint FROM idempotency_keys WHERE idempotency_key=?",
                (request.idempotency_key,),
            ).fetchone()
            request_fingerprint = hashlib.sha256(
                request.model_dump_json(exclude_none=True).encode()
            ).hexdigest()
            if existing_key:
                if existing_key["request_fingerprint"] != request_fingerprint:
                    raise WhatsAppConflict(
                        "Idempotency key was already used with another request"
                    )
                return self._assignment_payload(
                    connection, str(existing_key["assignment_id"])
                )

            params: list[Any] = []
            requested_sql = ""
            if request.requested_batch is not None:
                requested_sql = " AND batch_number=?"
                params.append(int(request.requested_batch))
            row = connection.execute(
                """
                SELECT * FROM media_batches
                WHERE media_state='complete'
                  AND ready_for_delivery=1
                  AND affiliate_delivery_state='unassigned'
                  AND current_assignment_id IS NULL
                """
                + requested_sql
                + " ORDER BY batch_number ASC LIMIT 1",
                params,
            ).fetchone()
            if not row:
                raise WhatsAppNotFound("No ready unassigned batch is available")
            assignment_id = uuid4().hex
            cursor = connection.execute(
                """
                UPDATE media_batches
                SET affiliate_delivery_state='assigned',
                    current_assignment_id=?,
                    version=version+1,
                    updated_at=?
                WHERE batch_number=?
                  AND media_state='complete'
                  AND ready_for_delivery=1
                  AND affiliate_delivery_state='unassigned'
                  AND current_assignment_id IS NULL
                """,
                (assignment_id, now, int(row["batch_number"])),
            )
            if cursor.rowcount != 1:
                raise WhatsAppConflict("Batch was claimed concurrently")
            connection.execute(
                """
                INSERT INTO affiliate_assignments(
                    assignment_id,batch_number,affiliate_name,affiliate_identifier,
                    assigned_at,delivery_status,idempotency_key,created_by,
                    campaign_or_sheet_row_identifier,version,updated_at
                ) VALUES(?,?,?,?,?,'assigned',?,?,?,0,?)
                """,
                (
                    assignment_id,
                    int(row["batch_number"]),
                    request.affiliate_name,
                    request.affiliate_identifier,
                    now,
                    request.idempotency_key,
                    actor,
                    request.campaign_or_sheet_row_identifier,
                    now,
                ),
            )
            files = connection.execute(
                "SELECT * FROM media_files WHERE batch_number=? ORDER BY ordinal",
                (int(row["batch_number"]),),
            ).fetchall()
            for item in files:
                connection.execute(
                    """
                    INSERT INTO delivery_items(
                        assignment_id,relative_path,status,attempt_count,updated_at
                    ) VALUES(?,?,'pending',0,?)
                    """,
                    (assignment_id, item["relative_path"], now),
                )
            connection.execute(
                "INSERT INTO idempotency_keys(idempotency_key,assignment_id,request_fingerprint,created_at) VALUES(?,?,?,?)",
                (
                    request.idempotency_key,
                    assignment_id,
                    request_fingerprint,
                    now,
                ),
            )
            self._event(
                connection,
                int(row["batch_number"]),
                assignment_id,
                "assigned",
                {"actor": actor},
            )
            self._outbox(connection, assignment_id, "assignment_upsert")
            return self._assignment_payload(connection, assignment_id)

    def transition(
        self,
        assignment_id: str,
        target: str,
        *,
        expected_version: int,
        actor: str,
        idempotency_key: str,
        error: str | None = None,
        drive_or_media_reference: str | None = None,
        operator_reason: str | None = None,
    ) -> dict[str, Any]:
        if target in {"sending", "sent", "delivery_failed"}:
            self._require_direct_delivery_cutover()
        allowed = {
            "assigned": {"sending", "cancelled"},
            "sending": {"sent", "delivery_failed"},
            "delivery_failed": {"sending", "cancelled"},
            "cancelled": {"unassigned"},
        }
        now = _utc_now()
        with self.transaction(immediate=True) as connection:
            transition_fingerprint = hashlib.sha256(
                json.dumps(
                    {
                        "assignment_id": assignment_id,
                        "target": target,
                        "expected_version": expected_version,
                        "error": error,
                        "drive_or_media_reference": drive_or_media_reference,
                        "operator_reason": operator_reason,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            prior = connection.execute(
                """
                SELECT request_fingerprint FROM delivery_action_idempotency
                WHERE idempotency_key=?
                """,
                (idempotency_key,),
            ).fetchone()
            if prior:
                if prior["request_fingerprint"] != transition_fingerprint:
                    raise WhatsAppConflict(
                        "Idempotency key was already used for another delivery action"
                    )
                return self._assignment_payload(connection, assignment_id)
            assignment = connection.execute(
                "SELECT * FROM affiliate_assignments WHERE assignment_id=?",
                (assignment_id,),
            ).fetchone()
            if not assignment:
                raise WhatsAppNotFound("Assignment was not found")
            current = str(assignment["delivery_status"])
            if target not in allowed.get(current, set()):
                raise WhatsAppConflict(f"Illegal delivery transition {current} -> {target}")
            if int(assignment["version"]) != int(expected_version):
                raise WhatsAppConflict("Assignment version changed")
            if target in {"cancelled", "unassigned"} and not operator_reason:
                raise WhatsAppConflict("Operator reason is required")
            confirmed = connection.execute(
                "SELECT COUNT(*) FROM delivery_items WHERE assignment_id=? AND whatsapp_message_id IS NOT NULL",
                (assignment_id,),
            ).fetchone()[0]
            uncertain = connection.execute(
                "SELECT COUNT(*) FROM delivery_items WHERE assignment_id=? AND outcome_uncertain=1",
                (assignment_id,),
            ).fetchone()[0]
            if target in {"cancelled", "unassigned"} and (confirmed or uncertain):
                raise WhatsAppConflict(
                    "Assignment cannot be released after a confirmed or uncertain send"
                )

            batch_number = int(assignment["batch_number"])
            if target == "sent":
                remaining = connection.execute(
                    "SELECT COUNT(*) FROM delivery_items WHERE assignment_id=? AND status!='sent'",
                    (assignment_id,),
                ).fetchone()[0]
                if remaining:
                    raise WhatsAppConflict("All delivery items must be sent first")
            if target == "unassigned":
                connection.execute(
                    """
                    UPDATE media_batches SET affiliate_delivery_state='unassigned',
                        current_assignment_id=NULL,version=version+1,updated_at=?
                    WHERE batch_number=? AND current_assignment_id=?
                    """,
                    (now, batch_number, assignment_id),
                )
                new_assignment_status = "cancelled"
            else:
                connection.execute(
                    """
                    UPDATE media_batches SET affiliate_delivery_state=?,
                        version=version+1,updated_at=?
                    WHERE batch_number=? AND current_assignment_id=?
                    """,
                    (target, now, batch_number, assignment_id),
                )
                new_assignment_status = target
            connection.execute(
                """
                UPDATE affiliate_assignments
                SET delivery_status=?,delivery_error=?,
                    drive_or_media_reference=COALESCE(?,drive_or_media_reference),
                    sent_at=?,
                    version=version+1,updated_at=?
                WHERE assignment_id=? AND version=?
                """,
                (
                    new_assignment_status,
                    error,
                    drive_or_media_reference,
                    now if target == "sent" else assignment["sent_at"],
                    now,
                    assignment_id,
                    expected_version,
                ),
            )
            self._event(
                connection,
                batch_number,
                assignment_id,
                target,
                {"actor": actor, "error": error, "operator_reason": operator_reason},
            )
            self._outbox(connection, assignment_id, "assignment_upsert")
            connection.execute(
                """
                INSERT INTO delivery_action_idempotency(
                    idempotency_key,assignment_id,request_fingerprint,created_at
                ) VALUES(?,?,?,?)
                """,
                (
                    idempotency_key,
                    assignment_id,
                    transition_fingerprint,
                    now,
                ),
            )
            return self._assignment_payload(connection, assignment_id)

    def update_item(
        self,
        assignment_id: str,
        relative_path: str,
        *,
        status: str,
        whatsapp_media_id: str | None = None,
        whatsapp_message_id: str | None = None,
        drive_or_media_reference: str | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        self._require_direct_delivery_cutover()
        allowed = {"pending", "uploading", "sent", "failed", "outcome_uncertain"}
        if status not in allowed:
            raise ValueError("Invalid delivery item status")
        now = _utc_now()
        with self.transaction(immediate=True) as connection:
            assignment = connection.execute(
                "SELECT * FROM affiliate_assignments WHERE assignment_id=?",
                (assignment_id,),
            ).fetchone()
            if not assignment:
                raise WhatsAppNotFound("Assignment was not found")
            if assignment["delivery_status"] not in {
                "assigned",
                "sending",
                "delivery_failed",
            }:
                raise WhatsAppConflict("Assignment is not sendable")
            existing_item = connection.execute(
                """
                SELECT * FROM delivery_items
                WHERE assignment_id=? AND relative_path=?
                """,
                (assignment_id, relative_path),
            ).fetchone()
            if not existing_item:
                raise WhatsAppNotFound("Delivery item was not found")
            if (
                existing_item["status"] == status
                and (not whatsapp_media_id or existing_item["whatsapp_media_id"] == whatsapp_media_id)
                and (
                    not whatsapp_message_id
                    or existing_item["whatsapp_message_id"] == whatsapp_message_id
                )
                and (
                    not drive_or_media_reference
                    or existing_item["drive_or_media_reference"] == drive_or_media_reference
                )
            ):
                return self._assignment_payload(connection, assignment_id)
            cursor = connection.execute(
                """
                UPDATE delivery_items SET status=?,
                    whatsapp_media_id=COALESCE(?,whatsapp_media_id),
                    whatsapp_message_id=COALESCE(?,whatsapp_message_id),
                    drive_or_media_reference=COALESCE(?,drive_or_media_reference),
                    attempt_count=attempt_count+1,last_error=?,
                    outcome_uncertain=?,updated_at=?
                WHERE assignment_id=? AND relative_path=?
                """,
                (
                    status,
                    whatsapp_media_id,
                    whatsapp_message_id,
                    drive_or_media_reference,
                    error,
                    int(status == "outcome_uncertain"),
                    now,
                    assignment_id,
                    relative_path,
                ),
            )
            if cursor.rowcount != 1:
                raise WhatsAppNotFound("Delivery item was not found")
            self._event(
                connection,
                int(assignment["batch_number"]),
                assignment_id,
                "delivery_item_updated",
                {"relative_path": relative_path, "status": status},
            )
            return self._assignment_payload(connection, assignment_id)

    def status(self, *, limit: int = 100) -> dict[str, Any]:
        self.ensure_schema()
        connection = self._connect()
        try:
            counts = {
                row["affiliate_delivery_state"]: int(row["count"])
                for row in connection.execute(
                    "SELECT affiliate_delivery_state,COUNT(*) count FROM media_batches GROUP BY affiliate_delivery_state"
                )
            }
            counts.update(
                {
                    f"media_{row['media_state']}": int(row["count"])
                    for row in connection.execute(
                        "SELECT media_state,COUNT(*) count FROM media_batches GROUP BY media_state"
                    )
                }
            )
            counts["ready_batches"] = int(
                connection.execute(
                    "SELECT COUNT(*) FROM media_batches WHERE ready_for_delivery=1"
                ).fetchone()[0]
            )
            rows = connection.execute(
                "SELECT assignment_id FROM affiliate_assignments ORDER BY assigned_at DESC LIMIT ?",
                (max(1, min(1000, int(limit))),),
            ).fetchall()
            return {
                "cutover": {
                    "direct_pc_delivery_enabled": self.direct_pc_delivery_enabled,
                    "legacy_drive_workflow_disabled": self.legacy_drive_workflow_disabled,
                    "claims_enabled": self.direct_claims_enabled,
                    "blocking_reason": (
                        None
                        if self.direct_claims_enabled
                        else (
                            "Direct PC delivery is disabled"
                            if not self.direct_pc_delivery_enabled
                            else "Legacy Drive workflow disablement is not confirmed"
                        )
                    ),
                },
                "counts": counts,
                "assignments": [
                    self._assignment_payload(connection, row["assignment_id"])
                    for row in rows
                ],
            }
        finally:
            connection.close()

    def pending_outbox(self, *, limit: int = 100) -> dict[str, Any]:
        self.ensure_schema()
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT o.*,a.batch_number,a.affiliate_name,a.affiliate_identifier,
                       a.delivery_status,a.assigned_at,a.sent_at,a.delivery_error,
                       a.drive_or_media_reference
                FROM sheet_sync_outbox o
                JOIN affiliate_assignments a ON a.assignment_id=o.assignment_id
                WHERE o.status IN ('pending','failed')
                ORDER BY o.created_at LIMIT ?
                """,
                (max(1, min(1000, int(limit))),),
            ).fetchall()
            return {"items": [dict(row) for row in rows]}
        finally:
            connection.close()

    def acknowledge_outbox(
        self, outbox_id: str, *, success: bool, error: str | None = None
    ) -> dict[str, Any]:
        now = _utc_now()
        with self.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE sheet_sync_outbox
                SET status=?,attempt_count=attempt_count+1,last_error=?,updated_at=?
                WHERE outbox_id=?
                """,
                ("synced" if success else "failed", error, now, outbox_id),
            )
            if cursor.rowcount != 1:
                raise WhatsAppNotFound("Sheet outbox event was not found")
            return dict(
                connection.execute(
                    "SELECT * FROM sheet_sync_outbox WHERE outbox_id=?",
                    (outbox_id,),
                ).fetchone()
            )

    def set_packaging_floor(self, max_historical_batch: int) -> int:
        value = max(0, int(max_historical_batch))
        with self.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT value FROM packaging_state WHERE key='max_historical_batch'"
            ).fetchone()
            if existing:
                value = max(value, int(existing["value"]))
            connection.execute(
                """
                INSERT INTO packaging_state(key,value,updated_at)
                VALUES('max_historical_batch',?,?)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value,updated_at=excluded.updated_at
                """,
                (str(value), _utc_now()),
            )
        return value

    def packaging_floor(self) -> int:
        self.ensure_schema()
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT value FROM packaging_state WHERE key='max_historical_batch'"
            ).fetchone()
            return int(row["value"]) if row else 0
        finally:
            connection.close()

    def _assignment_payload(
        self, connection: sqlite3.Connection, assignment_id: str
    ) -> dict[str, Any]:
        assignment = connection.execute(
            "SELECT * FROM affiliate_assignments WHERE assignment_id=?",
            (assignment_id,),
        ).fetchone()
        if not assignment:
            raise WhatsAppNotFound("Assignment was not found")
        batch = connection.execute(
            "SELECT * FROM media_batches WHERE batch_number=?",
            (assignment["batch_number"],),
        ).fetchone()
        files = connection.execute(
            """
            SELECT mf.relative_path,mf.size_bytes,mf.fingerprint,
                   di.status,di.whatsapp_media_id,di.whatsapp_message_id,
                   di.drive_or_media_reference,di.attempt_count,di.last_error,
                   di.outcome_uncertain
            FROM media_files mf
            LEFT JOIN delivery_items di
              ON di.assignment_id=? AND di.relative_path=mf.relative_path
            WHERE mf.batch_number=? ORDER BY mf.ordinal
            """,
            (assignment_id, assignment["batch_number"]),
        ).fetchall()
        return {
            "affiliate_assignment_id": assignment_id,
            "batch_number": int(assignment["batch_number"]),
            "affiliate_name": assignment["affiliate_name"],
            "affiliate_identifier": assignment["affiliate_identifier"],
            "assigned_at": assignment["assigned_at"],
            "sent_at": assignment["sent_at"],
            "delivery_status": assignment["delivery_status"],
            "delivery_error": assignment["delivery_error"],
            "drive_or_media_reference": assignment["drive_or_media_reference"],
            "idempotency_key": assignment["idempotency_key"],
            "version": int(assignment["version"]),
            "canonical_folder_path": batch["canonical_path"],
            "files": [dict(item) for item in files],
        }

    def _batch_payload(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> dict[str, Any]:
        files = connection.execute(
            "SELECT * FROM media_files WHERE batch_number=? ORDER BY ordinal",
            (row["batch_number"],),
        ).fetchall()
        return {**dict(row), "files": [dict(item) for item in files]}

    @staticmethod
    def _event(
        connection: sqlite3.Connection,
        batch_number: int,
        assignment_id: str | None,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        connection.execute(
            """
            INSERT INTO delivery_events(
                event_id,batch_number,assignment_id,event_type,payload_json,created_at
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                uuid4().hex,
                batch_number,
                assignment_id,
                event_type,
                json.dumps(payload, separators=(",", ":")),
                _utc_now(),
            ),
        )

    @staticmethod
    def _outbox(
        connection: sqlite3.Connection, assignment_id: str, event_type: str
    ) -> None:
        connection.execute(
            """
            INSERT INTO sheet_sync_outbox(
                outbox_id,assignment_id,event_type,status,attempt_count,created_at,updated_at
            ) VALUES(?,?,?,'pending',0,?,?)
            """,
            (uuid4().hex, assignment_id, event_type, _utc_now(), _utc_now()),
        )


_SCHEMA = """
CREATE TABLE IF NOT EXISTS whatsapp_meta(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS media_batches(
    batch_number INTEGER PRIMARY KEY,
    canonical_path TEXT NOT NULL UNIQUE,
    media_state TEXT NOT NULL,
    ready_for_delivery INTEGER NOT NULL DEFAULT 0,
    expected_file_count INTEGER NOT NULL,
    published_at TEXT,
    audit_fingerprint TEXT NOT NULL,
    current_assignment_id TEXT,
    affiliate_delivery_state TEXT NOT NULL DEFAULT 'unassigned',
    version INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS media_files(
    batch_number INTEGER NOT NULL REFERENCES media_batches(batch_number),
    relative_path TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    size_bytes INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    compliance_json TEXT NOT NULL,
    validated_at TEXT NOT NULL,
    PRIMARY KEY(batch_number,relative_path)
);
CREATE TABLE IF NOT EXISTS affiliate_assignments(
    assignment_id TEXT PRIMARY KEY,
    batch_number INTEGER NOT NULL REFERENCES media_batches(batch_number),
    affiliate_name TEXT NOT NULL,
    affiliate_identifier TEXT NOT NULL,
    assigned_at TEXT NOT NULL,
    sent_at TEXT,
    delivery_status TEXT NOT NULL,
    delivery_error TEXT,
    drive_or_media_reference TEXT,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL,
    campaign_or_sheet_row_identifier TEXT,
    version INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS delivery_items(
    assignment_id TEXT NOT NULL REFERENCES affiliate_assignments(assignment_id),
    relative_path TEXT NOT NULL,
    status TEXT NOT NULL,
    whatsapp_media_id TEXT,
    whatsapp_message_id TEXT,
    drive_or_media_reference TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    outcome_uncertain INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(assignment_id,relative_path)
);
CREATE TABLE IF NOT EXISTS delivery_events(
    event_id TEXT PRIMARY KEY,
    batch_number INTEGER NOT NULL,
    assignment_id TEXT,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS idempotency_keys(
    idempotency_key TEXT PRIMARY KEY,
    assignment_id TEXT NOT NULL REFERENCES affiliate_assignments(assignment_id),
    request_fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS delivery_action_idempotency(
    idempotency_key TEXT PRIMARY KEY,
    assignment_id TEXT NOT NULL REFERENCES affiliate_assignments(assignment_id),
    request_fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sheet_sync_outbox(
    outbox_id TEXT PRIMARY KEY,
    assignment_id TEXT NOT NULL REFERENCES affiliate_assignments(assignment_id),
    event_type TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS processing_runs(
    run_id TEXT PRIMARY KEY,
    canonical_source_path TEXT NOT NULL,
    canonical_destination_path TEXT NOT NULL,
    policy_revision TEXT NOT NULL,
    configuration_fingerprint TEXT NOT NULL,
    normalized_batch_selection TEXT NOT NULL,
    relevant_cli_options TEXT NOT NULL,
    created_at TEXT NOT NULL,
    heartbeat_at TEXT,
    owner_pid INTEGER,
    owner_host TEXT,
    status TEXT NOT NULL,
    parent_run_id TEXT,
    completed_at TEXT,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS processing_run_locks(
    destination_path TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES processing_runs(run_id),
    owner_pid INTEGER NOT NULL,
    owner_host TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS media_processing_files(
    run_id TEXT NOT NULL REFERENCES processing_runs(run_id),
    batch_number INTEGER NOT NULL,
    relative_path TEXT NOT NULL,
    source_size_bytes INTEGER NOT NULL,
    source_mtime_ns INTEGER NOT NULL,
    source_fast_fingerprint TEXT,
    status TEXT NOT NULL,
    classification TEXT,
    processing_action TEXT,
    probe_json TEXT,
    duration_seconds REAL,
    original_size_bytes INTEGER,
    final_size_bytes INTEGER,
    original_width INTEGER,
    original_height INTEGER,
    final_width INTEGER,
    final_height INTEGER,
    codec TEXT,
    profile TEXT,
    has_b_frames INTEGER,
    encoding_attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    started_at TEXT,
    completed_at TEXT,
    PRIMARY KEY(run_id,batch_number,relative_path)
);
CREATE TABLE IF NOT EXISTS processing_attempts(
    attempt_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    batch_number INTEGER NOT NULL,
    relative_path TEXT NOT NULL,
    attempt_number INTEGER NOT NULL,
    processing_action TEXT NOT NULL,
    target_video_bps INTEGER,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL,
    error TEXT,
    diagnostics_json TEXT
);
CREATE TABLE IF NOT EXISTS packaging_state(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS media_batches_claim_idx
    ON media_batches(media_state,ready_for_delivery,affiliate_delivery_state,batch_number);
CREATE INDEX IF NOT EXISTS delivery_events_assignment_idx
    ON delivery_events(assignment_id,created_at);
CREATE INDEX IF NOT EXISTS sheet_sync_pending_idx
    ON sheet_sync_outbox(status,created_at);
CREATE INDEX IF NOT EXISTS processing_runs_resume_idx
    ON processing_runs(status,canonical_destination_path,created_at);
CREATE INDEX IF NOT EXISTS media_processing_status_idx
    ON media_processing_files(run_id,status,batch_number);
"""
