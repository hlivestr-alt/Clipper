from __future__ import annotations

import csv
import json
import os
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from module_extractor import (
    PRODUCT_FOLDERS,
    QUALITY_APPROVED,
    QUALITY_NO_VISUAL_EVENTS,
    ROLE_FOLDERS,
    module_quality_fields,
    module_sidecar_path,
    read_library_index,
)
from module_readiness import build_product_readiness_from_index


def build_module_library_report(cfg, output_dir: str | Path | None = None) -> dict[str, Any]:
    index = read_library_index(cfg)
    library_dir = Path(output_dir or index.get("library_dir") or getattr(cfg, "MODULE_LIBRARY_DIR", r"D:\proya_modules"))
    modules = _load_modules(index, cfg)
    rejection_counts = _candidate_rejection_counts(Path(getattr(cfg, "WORKING_DIR", "working")))
    readiness = _readiness_by_product(modules, cfg)
    visual_readiness = _visual_readiness_by_product_role(modules, cfg)
    inventory_readiness = build_product_readiness_from_index(
        library_dir,
        int(getattr(cfg, "MODULAR_ASSEMBLY_READY_MIN_HOOK", 5) or 5),
        int(getattr(cfg, "MODULAR_ASSEMBLY_READY_MIN_MAIN", 3) or 3),
        int(getattr(cfg, "MODULAR_ASSEMBLY_READY_MIN_CTA", 3) or 3),
    )

    product_role_quality: Counter[tuple[str, str, str]] = Counter()
    product_role_boundary: Counter[tuple[str, str, str]] = Counter()
    product_role_usable: Counter[tuple[str, str, str]] = Counter()
    product_role_visual: Counter[tuple[str, str, str]] = Counter()
    product_sources: dict[str, set[str]] = defaultdict(set)
    sidecar_errors = 0

    for module in modules:
        product = str(module.get("product") or "")
        role = str(module.get("role") or "")
        quality = str(module.get("quality_status") or "")
        boundary = str(module.get("boundary_mode") or "unknown")
        visual = str(module.get("visual_validation_status") or "not_run")
        product_role_quality[(product, role, quality)] += 1
        product_role_boundary[(product, role, boundary)] += 1
        product_role_usable[(product, role, str(bool(module.get("usable_for_assembly"))).lower())] += 1
        product_role_visual[(product, role, visual)] += 1
        if module.get("sidecar_load_error"):
            sidecar_errors += 1
        source = _source_key(module)
        if source:
            product_sources[product].add(source)

    report = {
        "schema_version": 1,
        "library_dir": str(library_dir.resolve()),
        "module_count": len(modules),
        "index_module_count": index.get("module_count", len(index.get("modules", []) or [])),
        "config": {
            "assembly_require_approved": bool(getattr(cfg, "MODULE_ASSEMBLY_REQUIRE_APPROVED", True)),
            "word_fallback_review_required": bool(getattr(cfg, "MODULE_WORD_FALLBACK_REVIEW_REQUIRED", True)),
            "inventory_ready_min_hook": int(getattr(cfg, "MODULAR_ASSEMBLY_READY_MIN_HOOK", 5) or 0),
            "inventory_ready_min_main": int(getattr(cfg, "MODULAR_ASSEMBLY_READY_MIN_MAIN", 3) or 0),
            "inventory_ready_min_cta": int(getattr(cfg, "MODULAR_ASSEMBLY_READY_MIN_CTA", 3) or 0),
            "min_source_videos": int(getattr(cfg, "MODULE_ASSEMBLY_MIN_SOURCE_VIDEOS", 2) or 0),
        },
        "counts_by_product_role_quality": _counter_to_rows(product_role_quality, ("product", "role", "quality_status")),
        "counts_by_product_role_boundary": _counter_to_rows(product_role_boundary, ("product", "role", "boundary_mode")),
        "counts_by_product_role_usable": _counter_to_rows(product_role_usable, ("product", "role", "usable_for_assembly")),
        "counts_by_product_role_visual": _counter_to_rows(product_role_visual, ("product", "role", "visual_validation_status")),
        "sidecar_load_error_count": sidecar_errors,
        "source_video_counts": [
            {"product": product, "source_video_count": len(sources)}
            for product, sources in sorted(product_sources.items())
        ],
        "rejection_reasons": [
            {"reason": reason, "count": count}
            for reason, count in sorted(rejection_counts.items(), key=lambda item: (-item[1], item[0]))
        ],
        "readiness": readiness,
        "visual_readiness": visual_readiness,
        "inventory_readiness": inventory_readiness.get("rows", []),
    }

    json_path = library_dir / "library_report.json"
    csv_path = library_dir / "library_report.csv"
    _write_json_atomic(json_path, report)
    _write_csv(csv_path, report)
    report["json_path"] = str(json_path.resolve())
    report["csv_path"] = str(csv_path.resolve())
    return report


def _load_modules(index: dict[str, Any], cfg) -> list[dict[str, Any]]:
    modules = []
    load_sidecars = bool(getattr(cfg, "MODULE_REPORT_LOAD_SIDECARS", False))
    for summary in index.get("modules", []) or []:
        if not isinstance(summary, dict):
            continue
        sidecar_error = ""
        sidecar_payload = None
        sidecar = Path(str(summary.get("sidecar_path") or module_sidecar_path(Path(str(summary.get("file_path") or "")))))
        try:
            sidecar_payload = json.loads(sidecar.read_text(encoding="utf-8"))
        except Exception as exc:
            sidecar_error = str(exc)
        if load_sidecars:
            record = dict(sidecar_payload) if isinstance(sidecar_payload, dict) else dict(summary)
        else:
            record = dict(summary)
            if isinstance(sidecar_payload, dict) and "words" in sidecar_payload:
                record["words"] = sidecar_payload.get("words") or []
        record["sidecar_load_error"] = sidecar_error
        record.update(module_quality_fields(record, cfg))
        record["usable_for_assembly"] = _usable_for_assembly(record)
        modules.append(record)
    return modules


def _readiness_by_product(modules: list[dict[str, Any]], cfg) -> list[dict[str, Any]]:
    role_thresholds = {
        "hook": int(getattr(cfg, "MODULAR_ASSEMBLY_READY_MIN_HOOK", 5) or 0),
        "main": int(getattr(cfg, "MODULAR_ASSEMBLY_READY_MIN_MAIN", 3) or 0),
        "cta": int(getattr(cfg, "MODULAR_ASSEMBLY_READY_MIN_CTA", 3) or 0),
    }
    min_sources = int(getattr(cfg, "MODULE_ASSEMBLY_MIN_SOURCE_VIDEOS", 2) or 0)
    grouped: dict[str, dict[str, list[dict[str, Any]]]] = {
        product: {role: [] for role in ROLE_FOLDERS}
        for product in PRODUCT_FOLDERS
    }
    for module in modules:
        product = module.get("product")
        role = module.get("role")
        if (
            product in grouped
            and role in grouped[product]
            and _quality_allows_assembly(module.get("quality_status"))
            and module.get("usable_for_assembly")
        ):
            grouped[product][role].append(module)

    rows = []
    for product, roles in grouped.items():
        counts = {role: len(roles.get(role, [])) for role in ROLE_FOLDERS}
        sources = {_source_key(module) for role_modules in roles.values() for module in role_modules}
        sources.discard("")
        missing = [
            f"{role}<{role_thresholds.get(role, 0)}"
            for role, count in counts.items()
            if count < role_thresholds.get(role, 0)
        ]
        if len(sources) < min_sources:
            missing.append(f"sources<{min_sources}")
        rows.append(
            {
                "product": product,
                "ready": not missing,
                "approved_hook": counts["hook"],
                "approved_main": counts["main"],
                "approved_cta": counts["cta"],
                "usable_hook": counts["hook"],
                "usable_main": counts["main"],
                "usable_cta": counts["cta"],
                "source_video_count": len(sources),
                "reason": "ready" if not missing else ", ".join(missing),
            }
        )
    return rows


def _visual_readiness_by_product_role(modules: list[dict[str, Any]], cfg) -> list[dict[str, Any]]:
    min_events = max(1, int(getattr(cfg, "MODULE_ASSEMBLY_ZOOM_READY_MIN_EVENTS", 1) or 1))
    grouped: dict[str, dict[str, dict[str, int]]] = {
        product: {
            role: {
                "total_modules": 0,
                "approved_modules": 0,
                "passed": 0,
                "failed": 0,
                "not_run": 0,
                "role_zoom_ready_candidate_count": 0,
            }
            for role in ROLE_FOLDERS
        }
        for product in PRODUCT_FOLDERS
    }
    product_zoom_ready: Counter[str] = Counter()

    for module in modules:
        product = str(module.get("product") or "")
        role = str(module.get("role") or "")
        if product not in grouped or role not in grouped[product]:
            continue
        row = grouped[product][role]
        row["total_modules"] += 1
        approved = _quality_allows_assembly(module.get("quality_status"))
        if approved:
            row["approved_modules"] += 1
        status = _visual_status(module.get("visual_validation_status"))
        row[status] += 1
        if approved and status == "passed" and int(module.get("visual_product_hits") or 0) >= min_events:
            row["role_zoom_ready_candidate_count"] += 1
            product_zoom_ready[product] += 1

    rows = []
    for product in PRODUCT_FOLDERS:
        for role in ROLE_FOLDERS:
            counts = grouped[product][role]
            total = counts["total_modules"]
            validated = counts["passed"] + counts["failed"]
            coverage = round((validated / total) * 100.0, 1) if total else 0.0
            rows.append(
                {
                    "product": product,
                    "role": role,
                    "total_modules": total,
                    "approved_modules": counts["approved_modules"],
                    "passed": counts["passed"],
                    "failed": counts["failed"],
                    "not_run": counts["not_run"],
                    "visual_coverage_percent": coverage,
                    "role_zoom_ready_candidate_count": counts["role_zoom_ready_candidate_count"],
                    "zoom_ready_candidate_count": int(product_zoom_ready.get(product, 0)),
                }
            )
    return rows


def _usable_for_assembly(module: dict[str, Any]) -> bool:
    if not _quality_allows_assembly(module.get("quality_status")):
        return False
    if module.get("sidecar_load_error"):
        return False
    if not Path(str(module.get("file_path") or "")).exists():
        return False
    return any(
        isinstance(word, dict)
        and str(word.get("word", "")).strip()
        for word in (module.get("words") or [])
    )


def _quality_allows_assembly(status: Any) -> bool:
    return str(status or "").strip() in {QUALITY_APPROVED, QUALITY_NO_VISUAL_EVENTS}


def _candidate_rejection_counts(working_dir: Path) -> Counter[str]:
    counts: Counter[str] = Counter()
    if not working_dir.exists():
        return counts
    for path in working_dir.rglob("module_candidates.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for candidate in payload.get("candidates", []) or []:
            if not isinstance(candidate, dict):
                continue
            reason = candidate.get("rejection_reason")
            if reason:
                counts[str(reason)] += 1
    return counts


def _source_key(module: dict[str, Any]) -> str:
    identity = module.get("source_video_identity")
    if isinstance(identity, dict):
        key = "|".join(str(identity.get(part, "")) for part in ("path", "size", "mtime_ns"))
        if key.strip("|"):
            return key
    return str(module.get("source_video") or "")


def _counter_to_rows(counter: Counter[tuple], labels: tuple[str, ...]) -> list[dict[str, Any]]:
    rows = []
    for key, count in sorted(counter.items()):
        row = {label: value for label, value in zip(labels, key)}
        row["count"] = count
        rows.append(row)
    return rows


def _visual_status(value: Any) -> str:
    status = str(value or "not_run").strip().lower()
    if status in {"passed", "failed", "not_run"}:
        return status
    return "not_run"


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _write_csv(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "section",
                "product",
                "role",
                "quality_status",
                "boundary_mode",
                "usable_for_assembly",
                "visual_validation_status",
                "reason",
                "count",
                "ready",
                "source_video_count",
                "usable_hook",
                "usable_main",
                "usable_cta",
                "total_modules",
                "approved_modules",
                "passed",
                "failed",
                "not_run",
                "visual_coverage_percent",
                "role_zoom_ready_candidate_count",
                "zoom_ready_candidate_count",
            ],
            extrasaction="ignore",
        )
        writer.writeheader()
        for row in report.get("counts_by_product_role_quality", []):
            writer.writerow({"section": "product_role_quality", **row})
        for row in report.get("counts_by_product_role_boundary", []):
            writer.writerow({"section": "product_role_boundary", **row})
        for row in report.get("counts_by_product_role_usable", []):
            writer.writerow({"section": "product_role_usable", **row})
        for row in report.get("counts_by_product_role_visual", []):
            writer.writerow({"section": "product_role_visual", **row})
        for row in report.get("rejection_reasons", []):
            writer.writerow({"section": "rejection_reason", **row})
        for row in report.get("readiness", []):
            writer.writerow({"section": "readiness", **row})
        for row in report.get("visual_readiness", []):
            writer.writerow({"section": "visual_readiness", **row})
    os.replace(tmp, path)
