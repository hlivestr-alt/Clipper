from __future__ import annotations

import json
import statistics
import sys
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config

from clipper_app.modular_planner.library_reader import ScannerLibraryReader
from clipper_app.modular_planner.quality import JoinabilityEvaluator, joinability_inventory, normalize_transcript
from clipper_app.modular_planner.selection import ModularPlannerSelector


PRODUCTS = ("cleanser", "toner", "serum", "eye_cream", "mask", "skin_cream")
BROKEN_EXAMPLES = (
    "ya mendingan kalian ganti aja ke facial cleanser dari",
    "kalau mau ambil dari",
    "untuk etalase nomor",
)


def verification(selector: ModularPlannerSelector, rows: list[dict[str, object]], product: str) -> dict[str, object]:
    compositions, warnings, stats = selector.generate(
        segments=rows, product=product, requested_template="standard", actual_template="standard",
        cta_mode="use_cta", target_min_duration=45, target_max_duration=75,
        requested_count=20, starting_ordinal=1, seed=f"v11-quality-audit-{product}",
        approved_usage={}, current_run_usage={}, comparisons=[],
    )
    durations = [float(item["actual_duration"]) for item in compositions]
    selected = [item for composition in compositions for item in composition["items"]]
    usage = Counter(str(item["segment_id"]) for item in selected)
    broken_selected = []
    for item in selected:
        text = normalize_transcript(str(item["transcript_text"]))
        if any(text.endswith(example) for example in BROKEN_EXAMPLES) or bool(
            item.get("ranking_metadata", {}).get("joinability", {}).get("hard_unusable")
        ):
            broken_selected.append({"segment_id": str(item["segment_id"]), "transcript": item["transcript_text"]})
    hard_count = sum(
        counts["hard_excluded"] for counts in stats.get("joinability_inventory", {}).values()
    )
    return {
        "generated": len(compositions),
        "average_duration": round(statistics.mean(durations), 3) if durations else None,
        "minimum_duration": min(durations, default=None),
        "maximum_duration": max(durations, default=None),
        "distinct_vod_count": len({str(item["source_id"]) for item in selected}),
        "hard_excluded_inventory_count": hard_count,
        "selected_soft_boundary_penalties": stats.get("selected_contextual_boundaries", 0),
        "average_hook_benefits_continuity": stats.get("mean_hook_benefits_continuity", 0),
        "reused_segment_count": sum(count > 1 for count in usage.values()),
        "maximum_segment_use": max(usage.values(), default=0),
        "known_broken_examples_selected": broken_selected,
        "warnings": warnings,
    }


def main() -> None:
    library = ScannerLibraryReader(ROOT / "working" / "modular_library.sqlite3", config.QUEUE_INPUT_DIR)
    evaluator = JoinabilityEvaluator()
    selector = ModularPlannerSelector()
    inventory: dict[str, object] = {}
    hard_examples: dict[str, list[dict[str, object]]] = {}
    rows_by_product: dict[str, list[dict[str, object]]] = {}
    for product in PRODUCTS:
        rows = library.inventory(product)["segments"]
        rows_by_product[product] = rows
        inventory[product] = joinability_inventory(rows)
        hard_examples[product] = [
            {
                "segment_id": row["segment_id"], "role": row["role"],
                "transcript": row["transcript_text"],
                "reasons": list(evaluator.evaluate(row.get("transcript_text")).reason_codes),
            }
            for row in rows if evaluator.evaluate(row.get("transcript_text")).hard_unusable
        ][:20]
    print(json.dumps({
        "inventory": inventory,
        "hard_exclusion_examples": hard_examples,
        "verification": {
            product: verification(selector, rows_by_product[product], product)
            for product in ("serum", "cleanser")
        },
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
