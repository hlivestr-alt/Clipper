from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from .registry import ArtifactRegistry, canonical_json, utc_now


class RawLifecycleManager:
    """Ownership and conservative cleanup rules for newly created raw cuts."""

    def __init__(self, registry: ArtifactRegistry):
        self.registry = registry

    @classmethod
    def from_working_dir(cls, working_dir: str | Path) -> "RawLifecycleManager":
        return cls(ArtifactRegistry.from_working_dir(working_dir))

    def register_new(self, path: str | Path, *, owner_job: str, owner_stage: str = "ffmpeg") -> str:
        value = str(Path(path).resolve(strict=False))
        operation_id = uuid4().hex
        now = utc_now()
        with self.registry.transaction() as db:
            row = db.execute("SELECT operation_id FROM raw_intermediates WHERE path=?", (value,)).fetchone()
            if row:
                db.execute(
                    "UPDATE raw_intermediates SET owner_job=?,owner_stage=?,status='ACTIVE',retry_required=1,updated_at=? WHERE path=?",
                    (owner_job, owner_stage, now, value),
                )
                return str(row[0])
            db.execute(
                "INSERT INTO raw_intermediates VALUES(?,?,?,?,?,?,?,?,?,?)",
                (operation_id, value, owner_job, owner_stage, None, "ACTIVE", 1, now, now, "{}"),
            )
        return operation_id

    def mark_failed(self, path: str | Path, *, interrupted: bool = False) -> None:
        status = "INTERRUPTED_RETAINED" if interrupted else "FAILED_RETAINED"
        with self.registry.transaction() as db:
            db.execute(
                "UPDATE raw_intermediates SET status=?,retry_required=1,updated_at=? WHERE path=?",
                (status, utc_now(), str(Path(path).resolve(strict=False))),
            )

    def cleanup_after_manifest_commit(
        self,
        path: str | Path,
        *,
        successor_path: str | Path,
        manifest_path: str | Path,
        clip_id: str,
        validation: dict[str, Any] | None,
    ) -> bool:
        raw = Path(path).resolve(strict=False)
        successor = Path(successor_path).resolve(strict=False)
        manifest = Path(manifest_path).resolve(strict=False)
        with self.registry.transaction() as db:
            row = db.execute("SELECT * FROM raw_intermediates WHERE path=?", (str(raw),)).fetchone()
        if row is None:
            return False  # Historical/untracked is UNKNOWN and must remain.
        if not successor.is_file() or successor.stat().st_size <= 0:
            self.mark_failed(raw)
            return False
        if not validation or not bool(validation.get("compliant")):
            self.mark_failed(raw)
            return False
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            committed = any(
                isinstance(item, dict)
                and str(item.get("clip_id") or "") == str(clip_id)
                and str(item.get("status") or "").casefold() not in {"", "failed", "compliance_blocked"}
                for item in payload
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            committed = False
        if not committed:
            self.mark_failed(raw)
            return False
        evidence = {
            "successor_path": str(successor), "successor_size": successor.stat().st_size,
            "manifest_path": str(manifest), "clip_id": clip_id,
            "validation": validation,
        }
        with self.registry.transaction() as db:
            db.execute(
                "UPDATE raw_intermediates SET successor_path=?,status='ELIGIBLE_COMMITTED',retry_required=0,updated_at=?,cleanup_evidence_json=? WHERE path=?",
                (str(successor), utc_now(), canonical_json(evidence), str(raw)),
            )
        if raw.is_file():
            raw.unlink()
        with self.registry.transaction() as db:
            db.execute(
                "UPDATE raw_intermediates SET status='CLEANED',updated_at=? WHERE path=?",
                (utc_now(), str(raw)),
            )
        return True

    def collect_terminal_leftovers(self, *, dry_run: bool = True) -> list[dict[str, Any]]:
        # Phase 1 intentionally exposes no historical deletion mode.  The flag
        # is accepted for a stable future CLI but False is rejected.
        if not dry_run:
            raise PermissionError("Phase 1 raw collector is dry-run only")
        with self.registry.connect() as db:
            rows = db.execute(
                "SELECT * FROM raw_intermediates WHERE status='ELIGIBLE_COMMITTED' ORDER BY created_at"
            ).fetchall()
        return [dict(row) for row in rows]
