from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from .registry import ArtifactRegistry


RESOLVED = {"RESOLVED_EXACT", "RESOLVED_STRONG"}


@dataclass(frozen=True)
class ReconciliationResult:
    record_id: str
    media_id: str
    previous_path: str
    resolved_path: str | None
    classification: str
    reason: str
    evidence: dict[str, Any]
    applied: bool


class ModularProductionReconciler:
    """Resolve stale variant paths using media identity and durable move evidence."""

    def __init__(self, database_path: str | Path, registry: ArtifactRegistry):
        self.database_path = Path(database_path).resolve(strict=False)
        self.registry = registry

    def reconcile(self, *, apply: bool = True) -> dict[str, Any]:
        rows, jobs = self._load_rows()
        before_stale = sum(1 for row in rows if not Path(str(row["output_path"])).is_file())
        manifest_index, strong_index = self._manifest_index(jobs)
        results: list[ReconciliationResult] = []
        for row in rows:
            previous = Path(str(row["output_path"])).resolve(strict=False)
            if previous.is_file():
                continue
            record_id = f"{row['job_id']}:{row['composition_id']}:{row['variant_index']}"
            media_id = str(row["media_id"])
            candidates = {path for path in manifest_index.get(media_id, set()) if path.is_file()}
            strong_candidates = {path for path in strong_index.get(media_id, set()) if path.is_file()}
            evidence: dict[str, Any] = {
                "media_id": media_id,
                "manifest_candidates": sorted(map(str, candidates)),
                "domain_scoped_tier_candidates": sorted(map(str, strong_candidates)),
            }
            classification = "MISSING"
            reason = "No identity-backed current path was found."
            resolved: Path | None = None
            if len(candidates) == 1:
                resolved = next(iter(candidates)).resolve()
                classification = "RESOLVED_EXACT"
                reason = "Unique job manifest media_id/clip_id identity chain points to an existing file."
            elif len(candidates) > 1:
                classification = "AMBIGUOUS"
                reason = "The same media_id maps to multiple existing manifest paths."
            elif len(strong_candidates) == 1:
                resolved = next(iter(strong_candidates)).resolve()
                classification = "RESOLVED_STRONG"
                reason = "Unique exact filename under a known tier, constrained by job + manifest media_id/clip_id identity."
            elif len(strong_candidates) > 1:
                classification = "AMBIGUOUS"
                reason = "The identity-constrained filename exists in multiple lifecycle tiers."
            else:
                history = [
                    event for event in self.registry.publish_history_for_path(previous)
                    if str(event.get("state")) == "COMMITTED"
                    and Path(str(event.get("destination_path"))).is_file()
                    and str(event.get("owner_identity") or "") in {"", media_id}
                ]
                destinations = {Path(str(event["destination_path"])).resolve() for event in history}
                evidence["publish_operations"] = [str(event.get("operation_id")) for event in history]
                if len(destinations) == 1:
                    resolved = next(iter(destinations))
                    classification = "RESOLVED_STRONG"
                    reason = "Unique committed publish history resolves the previous path."
                elif len(destinations) > 1:
                    classification = "AMBIGUOUS"
                    reason = "Publish history contains multiple current destinations."
            should_apply = bool(apply and resolved is not None and classification in RESOLVED)
            result = ReconciliationResult(
                record_id, media_id, str(previous), str(resolved) if resolved else None,
                classification, reason, evidence, False,
            )
            self.registry.record_reconciliation({
                "domain": "modular_production_variant", "record_id": record_id,
                "classification": classification, "previous_path": str(previous),
                "resolved_path": str(resolved) if resolved else None, "reason": reason,
                "evidence": evidence, "applied": False,
            })
            if should_apply:
                self._update_path(row, previous, resolved)
                self.registry.record_reconciliation({
                    "domain": "modular_production_variant", "record_id": record_id,
                    "classification": classification, "previous_path": str(previous),
                    "resolved_path": str(resolved), "reason": reason,
                    "evidence": evidence, "applied": True,
                })
                result = ReconciliationResult(**{**asdict(result), "applied": True})
            results.append(result)
        after_rows, _ = self._load_rows()
        after_stale = sum(1 for row in after_rows if not Path(str(row["output_path"])).is_file())
        counts: dict[str, int] = defaultdict(int)
        for result in results:
            counts[result.classification] += 1
        return {
            "database": str(self.database_path),
            "apply_requested": bool(apply),
            "before_total_rows": len(rows),
            "before_stale_rows": before_stale,
            "after_stale_rows": after_stale,
            "updated_rows": sum(1 for row in results if row.applied),
            "classifications": dict(sorted(counts.items())),
            "results": [asdict(row) for row in results],
        }

    def _load_rows(self) -> tuple[list[sqlite3.Row], dict[str, sqlite3.Row]]:
        db = sqlite3.connect(f"file:{self.database_path.as_posix()}?mode=rw", uri=True, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            rows = list(db.execute("SELECT * FROM modular_production_variants ORDER BY job_id,composition_id,variant_index"))
            jobs = {str(row["job_id"]): row for row in db.execute("SELECT job_id,output_directory FROM modular_production_jobs")}
            return rows, jobs
        finally:
            db.close()

    @staticmethod
    def _manifest_index(jobs: dict[str, sqlite3.Row]) -> tuple[dict[str, set[Path]], dict[str, set[Path]]]:
        index: dict[str, set[Path]] = defaultdict(set)
        strong: dict[str, set[Path]] = defaultdict(set)
        for job in jobs.values():
            output_root = Path(str(job["output_directory"])).resolve(strict=False)
            manifest_path = output_root / "manifest.json"
            try:
                payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, list):
                continue
            clip_to_media: dict[str, str] = {}
            for row in payload:
                if not isinstance(row, dict) or not row.get("media_id"):
                    continue
                if row.get("clip_id"):
                    clip_to_media[str(row["clip_id"])] = str(row["media_id"])
                raw = Path(str(row.get("output_file") or ""))
                if not str(raw):
                    continue
                path = raw if raw.is_absolute() else output_root / raw
                index[str(row["media_id"])].add(path.resolve(strict=False))
                filename = path.name
                version_dir = str(row.get("version_dir") or Path(str(row.get("output_file") or "")).parent.name)
                if filename and version_dir:
                    for tier in ("export_ready", "review_needed", "rejected", "_pending"):
                        candidate = (output_root / tier / version_dir / filename).resolve(strict=False)
                        if candidate.is_file():
                            strong[str(row["media_id"])].add(candidate)
            # Scoring is the authoritative tier move owner.  Older manifests
            # can retain the pre-sort path, while scores_summary keeps the
            # exact job-local clip_id and current path.  The manifest bridges
            # that clip_id to immutable media_id; no basename inference occurs.
            scores_path = output_root / "scores_summary.json"
            try:
                scores_payload = json.loads(scores_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                scores_payload = {}
            score_rows = scores_payload.get("clips", []) if isinstance(scores_payload, dict) else []
            for score in score_rows:
                if not isinstance(score, dict):
                    continue
                media_id = clip_to_media.get(str(score.get("clip_id") or ""))
                if not media_id:
                    continue
                raw_path = Path(str(score.get("clip_path") or score.get("output_file") or ""))
                if not str(raw_path):
                    continue
                candidate = raw_path if raw_path.is_absolute() else output_root / raw_path
                candidate = candidate.resolve(strict=False)
                try:
                    candidate.relative_to(output_root)
                except ValueError:
                    continue
                index[media_id].add(candidate)
        return index, strong

    def _update_path(self, row: sqlite3.Row, previous: Path, resolved: Path) -> None:
        db = sqlite3.connect(self.database_path, timeout=15)
        try:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute(
                """UPDATE modular_production_variants SET output_path=?
                   WHERE job_id=? AND composition_id=? AND variant_index=? AND output_path=?""",
                (str(resolved), row["job_id"], row["composition_id"], row["variant_index"], str(row["output_path"])),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Variant path changed concurrently; reconciliation was not applied")
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()


def write_reconciliation_report(path: str | Path, summary: dict[str, Any]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Storage Path Reconciliation",
        "",
        "No media was deleted. Only deterministic, identity-backed database path updates were permitted.",
        "",
        "## Counts",
        "",
        f"- Production variant rows: {summary['before_total_rows']}",
        f"- Stale before: {summary['before_stale_rows']}",
        f"- Rows updated: {summary['updated_rows']}",
        f"- Stale after: {summary['after_stale_rows']}",
        "",
        "## Classifications",
        "",
    ]
    for key, value in summary.get("classifications", {}).items():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Evidence", ""])
    for row in summary.get("results", []):
        lines.append(
            f"- `{row['record_id']}` — **{row['classification']}** — "
            f"`{row['previous_path']}` -> `{row.get('resolved_path') or '-'}`; {row['reason']} "
            f"Applied: {row['applied']}."
        )
    lines.extend(["", "Ambiguous and missing records were left untouched.", ""])
    target.write_text("\n".join(lines), encoding="utf-8")
    return target
