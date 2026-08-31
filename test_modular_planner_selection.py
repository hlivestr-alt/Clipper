from __future__ import annotations

from collections import Counter

import pytest

from clipper_app.contracts.modular_planner_models import SUGGESTED_DURATION_DEFAULTS
from clipper_app.modular_planner.selection import (
    MAX_PARTIAL_NODES_PER_COMPOSITION,
    MAX_PROPOSALS_PER_COMPOSITION,
    MAX_ROLE_POOL,
    ModularPlannerSelector,
    effectively_same_timeline,
)


def segment(role: str, index: int, *, product: str = "serum", duration: float = 20.0, source: str | None = None):
    return {
        "segment_id": f"{product}-{role}-{index}", "scan_id": "scan", "scanner_generation": 1,
        "source_id": source or f"source-{role}-{index}", "vod_filename": f"{role}-{index}.mp4",
        "canonical_path": f"/vod/{role}-{index}.mp4", "file_size": 1, "mtime_ns": 1,
        "content_fingerprint": f"fingerprint-{role}-{index}", "product": product, "role": role,
        "start_seconds": index * 30.0, "end_seconds": index * 30.0 + duration,
        "duration_seconds": duration, "confidence": 0.8 + min(index, 19) / 100.0,
        "transcript_text": f"{role} transcript {index}", "reason": "accepted",
    }


def generate(rows, **overrides):
    values = {
        "segments": rows, "product": "serum", "requested_template": "standard",
        "actual_template": "standard", "cta_mode": "use_cta", "target_min_duration": 45,
        "target_max_duration": 75, "requested_count": 20, "starting_ordinal": 1,
        "seed": "stable-seed", "approved_usage": {}, "current_run_usage": {}, "comparisons": [],
    }
    values.update(overrides)
    return ModularPlannerSelector().generate(**values)


def test_scarce_cta_is_reused_only_after_unique_inventory_and_balanced():
    rows = [segment("hook", i) for i in range(20)]
    rows += [segment("benefits", i) for i in range(20)]
    rows += [segment("cta", i) for i in range(4)]
    compositions, warnings, _stats = generate(rows)
    assert len(compositions) == 20
    counts = Counter(
        item["segment_id"] for composition in compositions for item in composition["items"]
        if item["role"] == "cta"
    )
    assert len(counts) == 4
    assert max(counts.values()) - min(counts.values()) <= 1
    assert any(item["role"] == "cta" for item in warnings)
    assert len({item["segment_id"] for c in compositions[:4] for item in c["items"] if item["role"] == "cta"}) == 4


def test_no_segment_duplicates_within_composition_and_wrong_product_is_excluded():
    rows = [segment("hook", i) for i in range(4)] + [segment("benefits", i) for i in range(4)]
    rows += [segment("cta", i) for i in range(2)] + [segment("cta", 99, product="toner")]
    compositions, _, _ = generate(rows, requested_count=5)
    assert len(compositions) == 5
    for composition in compositions:
        ids = [item["segment_id"] for item in composition["items"]]
        assert len(ids) == len(set(ids))
        assert all(item["product"] == "serum" for item in composition["items"])


def test_cta_transcript_content_is_not_filtered_and_no_cta_omits_slot():
    rows = [segment("hook", 0), segment("benefits", 0), segment("cta", 0)]
    rows[-1]["transcript_text"] = "Hari ini Rp99.000 promo beli 2 gratis 1 selama live"
    with_cta, _, _ = generate(rows, requested_count=1)
    assert [item["role"] for item in with_cta[0]["items"]] == ["hook", "benefits", "cta"]
    without, _, _ = generate(
        rows, requested_count=1, cta_mode="no_cta", target_min_duration=30, target_max_duration=60,
    )
    assert [item["role"] for item in without[0]["items"]] == ["hook", "benefits"]


@pytest.mark.parametrize("transcript", [
    "CTA dengan harga Rp99.000",
    "Promo diskon 50 persen hari ini",
    "Beli 2 gratis 1 selama live",
])
def test_every_accepted_same_product_cta_remains_eligible(transcript):
    rows = [segment("hook", 0), segment("benefits", 0), segment("cta", 0)]
    rows[-1]["transcript_text"] = transcript
    compositions, _, _ = generate(rows, requested_count=1)
    assert compositions[0]["items"][-1]["transcript_text"] == transcript


def test_all_six_suggested_duration_defaults():
    assert SUGGESTED_DURATION_DEFAULTS == {
        ("standard", "use_cta"): (45.0, 75.0),
        ("standard", "no_cta"): (30.0, 60.0),
        ("ingredient", "use_cta"): (60.0, 90.0),
        ("ingredient", "no_cta"): (45.0, 75.0),
        ("benefit_focus", "use_cta"): (60.0, 90.0),
        ("benefit_focus", "no_cta"): (45.0, 75.0),
    }


def test_ingredient_and_benefit_focus_allow_role_aware_reuse():
    rows = [segment("hook", i) for i in range(10)] + [segment("benefits", i) for i in range(20)]
    rows += [segment("ingredients", i) for i in range(2)] + [segment("cta", i) for i in range(4)]
    ingredient, warnings, _ = generate(
        rows, requested_template="ingredient", actual_template="ingredient",
        target_min_duration=60, target_max_duration=90, requested_count=10,
    )
    assert len(ingredient) == 10
    assert any(warning.get("role") == "ingredients" for warning in warnings)
    focus, _, _ = generate(
        rows, requested_template="benefit_focus", actual_template="benefit_focus",
        target_min_duration=60, target_max_duration=90, requested_count=10,
    )
    assert len(focus) == 10
    assert all(c["items"][1]["segment_id"] != c["items"][2]["segment_id"] for c in focus)


def test_current_attempt_and_approved_history_exclusions_are_caller_scoped():
    rows = [segment("hook", i) for i in range(3)] + [segment("benefits", i) for i in range(3)]
    rows += [segment("cta", i) for i in range(2)]
    first, _, _ = generate(rows, requested_count=1)
    second, _, _ = generate(rows, requested_count=1, comparisons=first)
    assert second[0]["exact_signature"] != first[0]["exact_signature"]
    future_without_unapproved, _, _ = generate(rows, requested_count=1, comparisons=[])
    assert future_without_unapproved[0]["exact_signature"] == first[0]["exact_signature"]


def test_only_fully_equivalent_ordered_timeline_is_hard_near_duplicate():
    base = [segment("hook", 0), segment("benefits", 0), segment("cta", 0)]
    near = [dict(item) for item in base]
    near[0]["segment_id"] = "replacement-hook"
    near[0]["start_seconds"] += 1
    near[0]["end_seconds"] += 1
    assert effectively_same_timeline(base, near)
    changed = [dict(item) for item in base]
    changed[1] = segment("benefits", 5)
    assert not effectively_same_timeline(base, changed)


def test_search_limits_are_structurally_bounded():
    assert MAX_ROLE_POOL == 256
    assert MAX_PROPOSALS_PER_COMPOSITION == 128
    assert MAX_PARTIAL_NODES_PER_COMPOSITION == 512
    rows = [segment("hook", i) for i in range(300)] + [segment("benefits", i) for i in range(300)]
    compositions, _, stats = generate(
        rows, requested_count=3, cta_mode="no_cta", target_min_duration=30, target_max_duration=60,
    )
    assert len(compositions) == 3
    assert all(size <= MAX_ROLE_POOL for size in stats["pool_sizes"].values())
    assert stats["partial_nodes_evaluated"] <= 3 * MAX_PARTIAL_NODES_PER_COMPOSITION


def test_hard_unjoinable_segment_is_excluded_and_reported():
    rows = [segment("hook", 0), segment("benefits", 0), segment("benefits", 1), segment("cta", 0)]
    rows[1]["transcript_text"] = "kalian bisa ambil produk dari"
    rows[2]["transcript_text"] = "kulit kusam terlihat lebih cerah"
    compositions, _, stats = generate(rows, requested_count=1)
    assert len(compositions) == 1
    assert rows[1]["segment_id"] not in {item["segment_id"] for item in compositions[0]["items"]}
    assert stats["joinability_inventory"]["benefits"]["hard_excluded"] == 1


def test_cleaner_boundary_wins_when_other_factors_are_equivalent():
    rows = [segment("hook", 0), segment("hook", 1), segment("benefits", 0), segment("cta", 0)]
    rows[0]["transcript_text"] = "dan juga sudah aman banget untuk kulit"
    rows[1]["transcript_text"] = "kulit kering terasa lebih nyaman"
    rows[0]["confidence"] = rows[1]["confidence"] = 0.9
    compositions, _, _ = generate(rows, requested_count=1)
    hook = next(item for item in compositions[0]["items"] if item["role"] == "hook")
    assert hook["segment_id"] == rows[1]["segment_id"]
    assert hook["ranking_metadata"]["joinability"]["boundary_label"] == "Clean"


def test_hook_benefits_continuity_is_a_soft_tiebreaker_and_is_explainable():
    rows = [segment("hook", 0), segment("benefits", 0), segment("benefits", 1), segment("cta", 0)]
    rows[0]["transcript_text"] = "debu dan polusi bikin wajah kotor"
    rows[1]["transcript_text"] = "membersihkan kotoran dan debu dari wajah"
    rows[2]["transcript_text"] = "noda hitam membuat kulit tampak kusam"
    rows[1]["confidence"] = rows[2]["confidence"] = 0.9
    compositions, _, _ = generate(rows, requested_count=1)
    benefit = next(item for item in compositions[0]["items"] if item["role"] == "benefits")
    assert benefit["segment_id"] == rows[1]["segment_id"]
    metadata = compositions[0]["selection_metadata"]
    assert metadata["hook_benefits_continuity"] > 0
    assert metadata["score_components"]["hook_benefits_continuity"] > 0


def test_contextual_boundary_and_unrelated_pair_remain_eligible():
    rows = [segment("hook", 0), segment("benefits", 0), segment("cta", 0)]
    rows[0]["transcript_text"] = "dan juga sudah aman banget untuk dipakai"
    rows[1]["transcript_text"] = "noda hitam tampak lebih samar"
    compositions, _, stats = generate(rows, requested_count=1)
    assert len(compositions) == 1
    assert compositions[0]["items"][0]["ranking_metadata"]["joinability"]["boundary_label"] == "Contextual"
    assert compositions[0]["selection_metadata"]["hook_benefits_continuity"] == 0.0
    assert stats["selected_contextual_boundaries"] == 1


def test_quality_signals_do_not_weaken_hard_duration_constraint():
    rows = [segment("hook", 0, duration=10), segment("benefits", 0, duration=10), segment("cta", 0, duration=10)]
    compositions, warnings, _ = generate(rows, requested_count=1)
    assert compositions == []
    assert any(warning["code"] == "search_exhausted" for warning in warnings)


def test_v11_quality_ranking_is_seeded_and_deterministic():
    rows = [segment("hook", i) for i in range(4)] + [segment("benefits", i) for i in range(4)]
    rows += [segment("cta", i) for i in range(2)]
    first, first_warnings, first_stats = generate(rows, requested_count=4)
    second, second_warnings, second_stats = generate(rows, requested_count=4)
    assert first == second
    assert first_warnings == second_warnings
    assert first_stats == second_stats
