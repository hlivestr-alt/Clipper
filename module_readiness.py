from __future__ import annotations

import json
from pathlib import Path
from typing import Any


MODULE_PRODUCTS = (
    ("cleanser", "Cleanser"),
    ("toner", "Toner"),
    ("serum", "Serum"),
    ("eye_cream", "Eye Cream"),
    ("mask", "Mask"),
    ("skin_cream", "Skin Cream"),
)
MODULE_ROLES = ("hook", "main", "cta")


def build_product_readiness_from_index(
    library_dir: str | Path,
    min_hook: int,
    min_main: int,
    min_cta: int,
) -> dict[str, Any]:
    """Return per-product readiness using only index.json summaries."""
    index_path = Path(library_dir) / "index.json"
    payload: dict[str, Any] = {}
    error = ""
    if index_path.exists():
        try:
            loaded = json.loads(index_path.read_text(encoding="utf-8"))
            payload = loaded if isinstance(loaded, dict) else {}
        except Exception as exc:
            error = str(exc)

    modules = payload.get("modules", []) if isinstance(payload, dict) else []
    counts = {
        product: {role: 0 for role in MODULE_ROLES}
        for product, _label in MODULE_PRODUCTS
    }
    if isinstance(modules, list):
        for module in modules:
            if not isinstance(module, dict):
                continue
            product = str(module.get("product") or "")
            role = str(module.get("role") or "")
            if product in counts and role in counts[product]:
                counts[product][role] += 1

    rows = []
    for product, label in MODULE_PRODUCTS:
        role_counts = counts[product]
        total = sum(role_counts.values())
        if role_counts["hook"] >= min_hook and role_counts["main"] >= min_main and role_counts["cta"] >= min_cta:
            readiness = "ready"
        elif total > 0:
            readiness = "partial"
        else:
            readiness = "empty"
        rows.append(
            {
                "Product": label,
                "product_key": product,
                "Hook": role_counts["hook"],
                "Main": role_counts["main"],
                "CTA": role_counts["cta"],
                "Total": total,
                "Readiness": readiness,
            }
        )

    try:
        index_module_count = int(payload.get("module_count") or len(modules) or 0) if isinstance(payload, dict) else 0
    except (TypeError, ValueError):
        index_module_count = len(modules) if isinstance(modules, list) else 0

    return {
        "index_path": str(index_path),
        "index_exists": index_path.exists(),
        "index_updated_at": payload.get("updated_at", "") if isinstance(payload, dict) else "",
        "index_module_count": index_module_count,
        "error": error,
        "thresholds": {"hook": min_hook, "main": min_main, "cta": min_cta},
        "rows": rows,
    }


def build_visual_readiness_from_index(
    library_dir: str | Path,
    min_events: int = 1,
) -> dict[str, Any]:
    """Return per-product visual validation readiness using only index.json summaries."""
    index_path = Path(library_dir) / "index.json"
    payload: dict[str, Any] = {}
    error = ""
    if index_path.exists():
        try:
            loaded = json.loads(index_path.read_text(encoding="utf-8"))
            payload = loaded if isinstance(loaded, dict) else {}
        except Exception as exc:
            error = str(exc)

    modules = payload.get("modules", []) if isinstance(payload, dict) else []
    counts = {
        product: {
            "total": 0,
            "approved": 0,
            "passed": 0,
            "failed": 0,
            "not_run": 0,
            "zoom_ready_candidate_count": 0,
        }
        for product, _label in MODULE_PRODUCTS
    }
    min_events = max(1, int(min_events or 1))
    if isinstance(modules, list):
        for module in modules:
            if not isinstance(module, dict):
                continue
            product = str(module.get("product") or "")
            if product not in counts:
                continue
            row = counts[product]
            row["total"] += 1
            approved = str(module.get("quality_status") or "") in {"approved", "no_visual_events"}
            if approved:
                row["approved"] += 1
            status = _visual_status(module.get("visual_validation_status"))
            row[status] += 1
            try:
                hits = int(module.get("visual_product_hits") or 0)
            except (TypeError, ValueError):
                hits = 0
            if approved and status == "passed" and hits >= min_events:
                row["zoom_ready_candidate_count"] += 1

    rows = []
    for product, label in MODULE_PRODUCTS:
        row = counts[product]
        total = row["total"]
        validated = row["passed"] + row["failed"]
        coverage = round((validated / total) * 100.0, 1) if total else 0.0
        rows.append(
            {
                "Product": label,
                "product_key": product,
                "Total": total,
                "Approved": row["approved"],
                "Passed": row["passed"],
                "Failed": row["failed"],
                "Not Run": row["not_run"],
                "Visual Coverage %": coverage,
                "Zoom-ready Candidates": row["zoom_ready_candidate_count"],
                "Zoom Ready": row["zoom_ready_candidate_count"] >= min_events,
            }
        )

    try:
        index_module_count = int(payload.get("module_count") or len(modules) or 0) if isinstance(payload, dict) else 0
    except (TypeError, ValueError):
        index_module_count = len(modules) if isinstance(modules, list) else 0

    return {
        "index_path": str(index_path),
        "index_exists": index_path.exists(),
        "index_updated_at": payload.get("updated_at", "") if isinstance(payload, dict) else "",
        "index_module_count": index_module_count,
        "error": error,
        "min_events": min_events,
        "rows": rows,
    }


def _visual_status(value: Any) -> str:
    status = str(value or "not_run").strip().lower()
    if status in {"passed", "failed", "not_run"}:
        return status
    return "not_run"
