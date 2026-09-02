from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from .models import CleanupClassification, LifecycleClass
from .registry import ArtifactRegistry, utc_now


@dataclass(frozen=True)
class InventoryRecord:
    path: str
    size_bytes: int
    root_name: str
    category: str
    lifecycle_class: str
    known_owner: str | None
    reference_count: int | None
    blockers: tuple[str, ...]
    tracked: bool
    regenerable: bool | None
    proposed_retention: str
    cleanup_eligibility: str
    reason: str
    artifact_id: str | None = None


@dataclass(frozen=True)
class InventorySnapshot:
    scan_id: str
    generated_at: str
    roots: dict[str, str]
    records: tuple[InventoryRecord, ...]
    truncated: bool

    @property
    def total_bytes(self) -> int:
        return sum(row.size_bytes for row in self.records)


class StorageInventoryService:
    """Explicit metadata-only inventory; never invoked during application startup."""

    def __init__(self, registry: ArtifactRegistry):
        self.registry = registry

    def scan(self, roots: dict[str, str | Path], *, max_files: int | None = None) -> InventorySnapshot:
        scan_id = uuid4().hex
        artifacts = {str(Path(row["canonical_path"]).resolve(strict=False)).casefold(): row for row in self.registry.all_artifacts()}
        references = {
            row["artifact_id"]: len(self.registry.active_references(str(row["artifact_id"])))
            for row in artifacts.values()
        }
        records: list[InventoryRecord] = []
        cache_rows: list[tuple[Any, ...]] = []
        truncated = False
        normalized_roots = {name: str(Path(path).resolve(strict=False)) for name, path in roots.items()}
        for root_name, raw_root in roots.items():
            root = Path(raw_root).resolve(strict=False)
            if not root.exists():
                continue
            for path in _iter_files(root):
                if max_files is not None and len(records) >= max_files:
                    truncated = True
                    break
                try:
                    stat = path.stat()
                except OSError:
                    continue
                category = _category(path, root_name, root)
                artifact = artifacts.get(str(path.resolve(strict=False)).casefold())
                records.append(_record(path, stat.st_size, root_name, category, artifact, references))
                cache_rows.append((str(path.resolve(strict=False)), root_name, stat.st_size, stat.st_mtime_ns, category, scan_id))
                if len(cache_rows) >= 1000:
                    self._cache(cache_rows)
                    cache_rows.clear()
            if truncated:
                break
        if cache_rows:
            self._cache(cache_rows)
        return InventorySnapshot(scan_id, utc_now(), normalized_roots, tuple(records), truncated)

    def _cache(self, rows: list[tuple[Any, ...]]) -> None:
        with self.registry.transaction() as db:
            db.executemany(
                """INSERT INTO inventory_cache(path,root_name,size_bytes,mtime_ns,category,last_seen_scan)
                   VALUES(?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET
                   root_name=excluded.root_name,size_bytes=excluded.size_bytes,mtime_ns=excluded.mtime_ns,
                   category=excluded.category,last_seen_scan=excluded.last_seen_scan""",
                rows,
            )


class DryRunReclamationPlanner:
    def __init__(self, registry: ArtifactRegistry):
        self.registry = registry

    def plan(self, snapshot: InventorySnapshot) -> dict[str, Any]:
        candidates = []
        blocked_examples = []
        classification_counts: dict[str, int] = {}
        total = 0
        for record in snapshot.records:
            classification_counts[record.cleanup_eligibility] = classification_counts.get(record.cleanup_eligibility, 0) + 1
            row = asdict(record)
            row["references_checked"] = record.reference_count
            row["proposed_action"] = "KEEP"
            if record.cleanup_eligibility == CleanupClassification.SAFE_CANDIDATE.value:
                row["proposed_action"] = "QUARANTINE_AFTER_RECHECK"
                total += record.size_bytes
                candidates.append(row)
            elif len(blocked_examples) < 200:
                blocked_examples.append(row)
        return {
            "schema_version": 1,
            "dry_run": True,
            "historical_deletion_performed": False,
            "scan_id": snapshot.scan_id,
            "generated_at": utc_now(),
            "safe_candidate_bytes": total,
            "candidate_count": len(candidates),
            "inventory_file_count": len(snapshot.records),
            "classification_counts": dict(sorted(classification_counts.items())),
            "items": candidates,
            "blocked_examples": blocked_examples,
        }

    @staticmethod
    def write(path: str | Path, plan: dict[str, Any]) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(target.suffix + ".tmp")
        temp.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, target)
        return target

    def execute(self, _plan: dict[str, Any]) -> None:
        raise PermissionError("Phase 1 reclamation plans are dry-run only")


def _iter_files(root: Path) -> Iterable[Path]:
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    yield Path(entry.path)
            except OSError:
                continue


def _category(path: Path, root_name: str, root: Path) -> str:
    relative = str(path.relative_to(root)).replace("\\", "/").casefold()
    if "transcript" in path.name.casefold():
        return "transcript"
    if "/raw_cuts/" in f"/{relative}" or path.name.casefold().endswith("_raw.mp4"):
        return "raw_intermediate"
    if root_name == "output_clips":
        for tier in ("export_ready", "review_needed", "rejected", "_pending", "export_batches"):
            if f"/{tier}/" in f"/{relative}/":
                return tier
        return "generated_output"
    if root_name == "vod":
        return "source_video"
    if "/dist" in f"/{relative}" or "win-unpacked" in relative or path.suffix.casefold() == ".exe":
        return "development_build"
    if path.suffix.casefold() in {".log", ".tmp", ".partial"}:
        return "log_or_temp"
    return "project_data"


def _record(
    path: Path, size: int, root_name: str, category: str,
    artifact: dict[str, Any] | None, references: dict[str, int],
) -> InventoryRecord:
    if artifact is None:
        return InventoryRecord(
            str(path.resolve(strict=False)), size, root_name, category, LifecycleClass.UNKNOWN.value,
            None, None, ("unknown_owner",), False, None, "KEEP",
            CleanupClassification.BLOCKED_UNKNOWN_OWNER.value,
            "Legacy/untracked artifacts default to KEEP until ownership is reconciled.", None,
        )
    lifecycle = LifecycleClass(str(artifact["lifecycle_class"]))
    ref_count = references.get(str(artifact["artifact_id"]), 0)
    if lifecycle == LifecycleClass.FINAL:
        classification, reason = CleanupClassification.BLOCKED_FINAL, "Final media is pinned."
    elif lifecycle == LifecycleClass.EXPORT:
        classification, reason = CleanupClassification.BLOCKED_EXPORT, "Export media is pinned while required."
    elif lifecycle == LifecycleClass.PENDING:
        classification, reason = CleanupClassification.BLOCKED_PENDING, "Pending media is pinned."
    elif lifecycle == LifecycleClass.SOURCE:
        classification, reason = CleanupClassification.BLOCKED_SOURCE_POLICY, "Source retention policy blocks cleanup."
    elif bool(artifact.get("pinned")) or ref_count:
        classification, reason = CleanupClassification.BLOCKED_ACTIVE_REFERENCE, "Pinned or actively referenced."
    elif lifecycle == LifecycleClass.REGENERABLE:
        evidence = _loads(artifact.get("regeneration_evidence_json"))
        source = evidence.get("source") or evidence.get("source_video")
        if artifact.get("regenerable") == 1 and source and Path(str(source)).is_file():
            classification, reason = CleanupClassification.SAFE_CANDIDATE, "Tracked, unreferenced, and required regeneration source exists."
        else:
            classification, reason = CleanupClassification.BLOCKED_AMBIGUOUS_REFERENCE, "Regeneration dependencies are incomplete or missing."
    elif lifecycle == LifecycleClass.CACHE:
        classification, reason = CleanupClassification.SAFE_CANDIDATE, "Tracked lifecycle and zero active references provide positive eligibility evidence."
    elif lifecycle == LifecycleClass.TEMP:
        evidence = _loads(artifact.get("regeneration_evidence_json"))
        if bool(evidence.get("terminal_successor_committed")):
            classification, reason = CleanupClassification.SAFE_CANDIDATE, "Tracked temporary artifact has a durably committed validated successor."
        else:
            classification, reason = CleanupClassification.BLOCKED_ACTIVE_REFERENCE, "Temporary artifact lacks terminal successor evidence and may be needed for retry."
    elif lifecycle in {LifecycleClass.REVIEW, LifecycleClass.REJECTED}:
        evidence = _loads(artifact.get("regeneration_evidence_json"))
        try:
            retention_days = int(evidence["retention_days"])
            created = datetime.fromisoformat(str(artifact["created_at"]))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            expired = datetime.now(timezone.utc) >= created.astimezone(timezone.utc) + timedelta(days=retention_days)
        except (KeyError, TypeError, ValueError):
            expired = False
        if expired:
            classification, reason = CleanupClassification.SAFE_CANDIDATE, "Tracked lifecycle retention policy expired with zero active references."
        else:
            classification, reason = CleanupClassification.LEGACY_REVIEW_REQUIRED, "Review/rejected retention window is active or not configured."
    else:
        classification, reason = CleanupClassification.LEGACY_REVIEW_REQUIRED, "Lifecycle requires explicit retention review."
    return InventoryRecord(
        str(path.resolve(strict=False)), size, root_name, category, lifecycle.value,
        artifact.get("owner_identity"), ref_count, tuple(), True,
        None if artifact.get("regenerable") is None else bool(artifact.get("regenerable")),
        "KEEP" if classification != CleanupClassification.SAFE_CANDIDATE else "POLICY_ELIGIBLE",
        classification.value, reason, str(artifact["artifact_id"]),
    )


def _loads(value: Any) -> dict[str, Any]:
    try:
        payload = json.loads(str(value or "{}"))
        return payload if isinstance(payload, dict) else {}
    except (ValueError, TypeError, json.JSONDecodeError):
        return {}
