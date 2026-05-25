from __future__ import annotations

import csv
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from module_extractor import (
    QUALITY_APPROVED,
    QUALITY_BLOCKED,
    QUALITY_NEEDS_REVIEW,
    REVIEW_PENDING,
    library_index_lock,
    module_file_lock,
    module_quality_fields,
    module_sidecar_path,
    rebuild_library_index,
)


VALID_REVIEW_STATUSES = {QUALITY_APPROVED, QUALITY_NEEDS_REVIEW, QUALITY_BLOCKED}
QUEUE_SCHEMA_VERSION = 1


def build_module_review_queue(
    cfg,
    status: str = QUALITY_NEEDS_REVIEW,
    product: str | None = None,
    role: str | None = None,
    limit: int | None = None,
    output_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Write a review queue JSON/CSV from current module sidecars."""
    library = Path(getattr(cfg, "MODULE_LIBRARY_DIR", r"D:\proya_modules"))
    with library_index_lock(library, cfg):
        index = rebuild_library_index(library, cfg, write=True)

    rows = []
    sidecar_errors = 0
    for summary in index.get("modules", []) or []:
        if not isinstance(summary, dict):
            continue
        record, error = _load_sidecar(summary)
        if error:
            sidecar_errors += 1
            record = dict(summary)
            record["sidecar_load_error"] = error
        record.update(module_quality_fields(record, cfg))
        if not _matches_filter(record, status=status, product=product, role=role):
            continue
        rows.append(_queue_row(record))

    rows.sort(key=lambda row: _queue_sort_key(row))
    if limit is not None:
        rows = rows[: max(0, int(limit))]

    output_root = Path(output_dir or library)
    json_path = output_root / "module_review_queue.json"
    csv_path = output_root / "module_review_queue.csv"
    result = {
        "schema_version": QUEUE_SCHEMA_VERSION,
        "created_at": _now_iso(),
        "library_dir": str(library.resolve()),
        "filter": {
            "status": status,
            "product": product or "",
            "role": role or "",
            "limit": limit,
        },
        "module_count": len(rows),
        "sidecar_load_error_count": sidecar_errors,
        "counts_by_quality_status": _counter_rows(Counter(row.get("quality_status", "") for row in rows), "quality_status"),
        "counts_by_product_role": _counter_rows(
            Counter((row.get("product", ""), row.get("role", "")) for row in rows),
            ("product", "role"),
        ),
        "modules": rows,
    }
    _write_json_atomic(json_path, result)
    _write_csv(csv_path, rows)
    result["json_path"] = str(json_path.resolve())
    result["csv_path"] = str(csv_path.resolve())
    return result


def update_module_review(
    identifier: str | Path,
    review_status: str,
    cfg,
    note: str = "",
    reviewer: str = "operator",
) -> dict[str, Any]:
    """Approve, block, or send a module back to review by updating its sidecar."""
    review_status = str(review_status or "").strip().lower()
    if review_status not in VALID_REVIEW_STATUSES:
        raise ValueError(f"review_status must be one of: {', '.join(sorted(VALID_REVIEW_STATUSES))}")

    library = Path(getattr(cfg, "MODULE_LIBRARY_DIR", r"D:\proya_modules"))
    sidecar_path = _resolve_sidecar_path(identifier, library, cfg)
    lock_basis = sidecar_path.with_suffix(".mp4")
    lock_path = lock_basis.with_suffix(lock_basis.suffix + ".lock")

    with module_file_lock(lock_path, cfg):
        record = _read_json(sidecar_path)
        if not isinstance(record, dict):
            raise FileNotFoundError(f"Module sidecar is missing or unreadable: {sidecar_path}")
        before = {
            "review_status": record.get("review_status", REVIEW_PENDING),
            "quality_status": record.get("quality_status", ""),
            "quality_reason": record.get("quality_reason", ""),
        }
        _apply_review_status(record, review_status, note=note, reviewer=reviewer, cfg=cfg)
        _write_json_atomic(sidecar_path, record)

    with library_index_lock(library, cfg):
        index = rebuild_library_index(library, cfg, write=True)

    return {
        "module_id": record.get("module_id") or sidecar_path.stem,
        "sidecar_path": str(sidecar_path.resolve()),
        "file_path": str(Path(str(record.get("file_path") or sidecar_path.with_suffix(".mp4"))).resolve()),
        "review_status": record.get("review_status"),
        "quality_status": record.get("quality_status"),
        "quality_reason": record.get("quality_reason"),
        "before": before,
        "index_module_count": index.get("module_count", 0),
    }


def _apply_review_status(record: dict[str, Any], review_status: str, note: str, reviewer: str, cfg) -> None:
    record["review_status"] = QUALITY_APPROVED if review_status == QUALITY_APPROVED else review_status
    record["reviewed_at"] = _now_iso()
    record["reviewer"] = str(reviewer or "operator").strip() or "operator"
    if note:
        record["review_note"] = str(note)

    record.pop("quality_score", None)
    if review_status == QUALITY_APPROVED:
        record.pop("quality_status", None)
        record.pop("quality_reason", None)
    elif review_status == QUALITY_BLOCKED:
        record["quality_status"] = QUALITY_BLOCKED
        record["quality_reason"] = "manual_review_blocked"
    else:
        record["review_status"] = REVIEW_PENDING
        record["quality_status"] = QUALITY_NEEDS_REVIEW
        record["quality_reason"] = "manual_review_requested"
    record.update(module_quality_fields(record, cfg))


def _resolve_sidecar_path(identifier: str | Path, library: Path, cfg) -> Path:
    raw = str(identifier or "").strip()
    if not raw:
        raise ValueError("Module identifier is required")

    direct = Path(raw)
    if direct.exists():
        return direct if direct.suffix.lower() == ".json" else module_sidecar_path(direct)

    with library_index_lock(library, cfg):
        index = rebuild_library_index(library, cfg, write=True)

    matches = []
    wanted = raw.lower()
    for summary in index.get("modules", []) or []:
        if not isinstance(summary, dict):
            continue
        module_id = str(summary.get("module_id") or "").lower()
        file_path = Path(str(summary.get("file_path") or ""))
        if wanted in {module_id, file_path.stem.lower(), file_path.name.lower()}:
            matches.append(Path(str(summary.get("sidecar_path") or module_sidecar_path(file_path))))
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(f"Module identifier is ambiguous: {raw}")

    glob_matches = list(library.rglob(f"{raw}.json"))
    if len(glob_matches) == 1:
        return glob_matches[0]
    if len(glob_matches) > 1:
        raise ValueError(f"Module identifier is ambiguous: {raw}")
    raise FileNotFoundError(f"Module not found in library: {raw}")


def _matches_filter(record: dict[str, Any], status: str, product: str | None, role: str | None) -> bool:
    if status and status != "all" and record.get("quality_status") != status:
        return False
    if product and record.get("product") != product:
        return False
    if role and record.get("role") != role:
        return False
    return True


def _queue_row(record: dict[str, Any]) -> dict[str, Any]:
    text = str(record.get("transcript_text") or "").strip()
    return {
        "module_id": record.get("module_id", ""),
        "product": record.get("product", ""),
        "role": record.get("role", ""),
        "quality_status": record.get("quality_status", ""),
        "quality_reason": record.get("quality_reason", ""),
        "review_status": record.get("review_status", REVIEW_PENDING),
        "visual_validation_status": record.get("visual_validation_status", "not_run"),
        "visual_product_hits": record.get("visual_product_hits", 0),
        "visual_validation_reason": record.get("visual_validation_reason", ""),
        "boundary_mode": record.get("boundary_mode", ""),
        "duration": record.get("duration", 0.0),
        "confidence": record.get("confidence", 0.0),
        "source_video": record.get("source_video", ""),
        "source_moment_id": record.get("source_moment_id", ""),
        "file_path": record.get("file_path", ""),
        "sidecar_path": str(Path(str(record.get("sidecar_path") or module_sidecar_path(Path(str(record.get("file_path") or ""))))).resolve()),
        "transcript_preview": text[:240],
        "review_note": record.get("review_note", ""),
    }


def _queue_sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
    priority = {
        "word_boundary_fallback_requires_review": 0,
        "product_evidence_unverified": 1,
        "manual_review_requested": 2,
    }
    return (
        priority.get(str(row.get("quality_reason") or ""), 10),
        row.get("product", ""),
        row.get("role", ""),
        -float(row.get("confidence") or 0.0),
        row.get("module_id", ""),
    )


def _load_sidecar(summary: dict[str, Any]) -> tuple[dict[str, Any], str]:
    sidecar = Path(str(summary.get("sidecar_path") or module_sidecar_path(Path(str(summary.get("file_path") or "")))))
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except Exception as exc:
        return {}, str(exc)
    if not isinstance(payload, dict):
        return {}, "sidecar payload is not an object"
    payload.setdefault("sidecar_path", str(sidecar))
    return payload, ""


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _counter_rows(counter: Counter, labels: str | tuple[str, ...]) -> list[dict[str, Any]]:
    if isinstance(labels, str):
        labels = (labels,)
    rows = []
    for key, count in sorted(counter.items()):
        if not isinstance(key, tuple):
            key = (key,)
        row = {label: value for label, value in zip(labels, key)}
        row["count"] = count
        rows.append(row)
    return rows


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fieldnames = [
        "module_id",
        "product",
        "role",
        "quality_status",
        "quality_reason",
        "review_status",
        "visual_validation_status",
        "visual_product_hits",
        "visual_validation_reason",
        "boundary_mode",
        "duration",
        "confidence",
        "source_video",
        "source_moment_id",
        "file_path",
        "sidecar_path",
        "transcript_preview",
        "review_note",
    ]
    with tmp.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    os.replace(tmp, path)


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
