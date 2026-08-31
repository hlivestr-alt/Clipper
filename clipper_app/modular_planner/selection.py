from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from typing import Any, Sequence

from clipper_app.modular_planner.quality import (
    JoinabilityEvaluator,
    composition_continuity,
    joinability_inventory,
)

PLANNER_VERSION = "modular-planner-v1.1"
SIGNATURE_VERSION = "modular-signature-v1"
MAX_ROLE_POOL = 256
MAX_PROPOSALS_PER_COMPOSITION = 128
MAX_PARTIAL_NODES_PER_COMPOSITION = 512


TEMPLATE_ROLES = {
    "standard": ("hook", "benefits"),
    "ingredient": ("hook", "ingredients", "benefits"),
    "benefit_focus": ("hook", "benefits", "benefits"),
}


def required_roles(template: str, cta_mode: str) -> tuple[str, ...]:
    roles = TEMPLATE_ROLES[template]
    return (*roles, "cta") if cta_mode == "use_cta" else roles


def _hash_int(*parts: Any) -> int:
    payload = "|".join(str(part) for part in parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def exact_signature(items: Sequence[dict[str, Any]]) -> str:
    value = [SIGNATURE_VERSION, *[(item["role"], item["segment_id"]) for item in items]]
    return hashlib.sha256(json.dumps(value, separators=(",", ":")).encode()).hexdigest()


def near_signature(items: Sequence[dict[str, Any]]) -> str:
    value = [
        SIGNATURE_VERSION,
        *[
            (
                item["role"], item["source_id"],
                round(float(item["start_seconds"])), round(float(item["end_seconds"])),
            )
            for item in items
        ],
    ]
    return hashlib.sha256(json.dumps(value, separators=(",", ":")).encode()).hexdigest()


def temporal_iou(left: dict[str, Any], right: dict[str, Any]) -> float:
    start = max(float(left["start_seconds"]), float(right["start_seconds"]))
    end = min(float(left["end_seconds"]), float(right["end_seconds"]))
    intersection = max(0.0, end - start)
    union = max(float(left["end_seconds"]), float(right["end_seconds"])) - min(
        float(left["start_seconds"]), float(right["start_seconds"])
    )
    return intersection / union if union > 0 else 0.0


def position_similarity(left: dict[str, Any], right: dict[str, Any]) -> float:
    if left["role"] != right["role"]:
        return 0.0
    if left["segment_id"] == right["segment_id"]:
        return 1.0
    if left["source_id"] != right["source_id"]:
        return 0.0
    if (
        abs(float(left["start_seconds"]) - float(right["start_seconds"])) <= 2.0
        and abs(float(left["end_seconds"]) - float(right["end_seconds"])) <= 2.0
    ):
        return 1.0
    return temporal_iou(left, right)


def timeline_similarity(left: Sequence[dict[str, Any]], right: Sequence[dict[str, Any]]) -> float:
    if len(left) != len(right) or [item["role"] for item in left] != [item["role"] for item in right]:
        return 0.0
    return sum(position_similarity(a, b) for a, b in zip(left, right)) / len(left)


def effectively_same_timeline(left: Sequence[dict[str, Any]], right: Sequence[dict[str, Any]]) -> bool:
    if len(left) != len(right) or [item["role"] for item in left] != [item["role"] for item in right]:
        return False
    return all(position_similarity(a, b) >= 0.80 for a, b in zip(left, right))


class ModularPlannerSelector:
    def __init__(self, *, max_role_pool: int = MAX_ROLE_POOL, max_proposals: int = MAX_PROPOSALS_PER_COMPOSITION):
        self.max_role_pool = max(1, min(MAX_ROLE_POOL, int(max_role_pool)))
        self.max_proposals = max(1, min(MAX_PROPOSALS_PER_COMPOSITION, int(max_proposals)))

    def generate(
        self,
        *,
        segments: Sequence[dict[str, Any]],
        product: str,
        requested_template: str,
        actual_template: str,
        cta_mode: str,
        target_min_duration: float,
        target_max_duration: float,
        requested_count: int,
        starting_ordinal: int,
        seed: str,
        approved_usage: dict[str, dict[str, Any]],
        current_run_usage: dict[str, int],
        comparisons: Sequence[dict[str, Any]],
        fallback_reason: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
        roles = required_roles(actual_template, cta_mode)
        evaluator = JoinabilityEvaluator()
        product_segments = [item for item in segments if item["product"] == product]
        quality_inventory = joinability_inventory(product_segments)
        eligible: list[dict[str, Any]] = []
        for item in product_segments:
            assessment = evaluator.evaluate(item.get("transcript_text"))
            if assessment.hard_unusable:
                continue
            annotated = dict(item)
            annotated["_joinability"] = assessment.as_dict()
            eligible.append(annotated)
        pools = self._candidate_pools(eligible, set(roles), approved_usage, seed)
        missing = [role for role in set(roles) if not pools.get(role)]
        if missing:
            warning = {"code": "missing_role_inventory", "roles": sorted(missing)}
            stats = self._statistics(0, 0, 0, pools)
            stats["joinability_inventory"] = quality_inventory
            return [], [warning], stats
        duration_bounds = {
            role: (
                min(float(item["duration_seconds"]) for item in pool),
                max(float(item["duration_seconds"]) for item in pool),
            )
            for role, pool in pools.items()
        }

        usage = Counter({key: int(value) for key, value in current_run_usage.items()})
        known = [value for value in comparisons]
        known_exact = {value["exact_signature"] for value in known}
        known_by_first_source: dict[tuple[tuple[str, ...], str], list[dict[str, Any]]] = defaultdict(list)
        known_by_source: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        for value in known:
            self._index_comparison(value, known_by_first_source, known_by_source)
        generated: list[dict[str, Any]] = []
        proposals_evaluated = partial_nodes = duration_rejections = duplicate_rejections = 0

        for offset in range(requested_count):
            ordinal = starting_ordinal + offset
            ranked = {
                role: sorted(
                    pool,
                    key=lambda item: (
                        usage[item["segment_id"]],
                        int(approved_usage.get(item["segment_id"], {}).get("usage_count", 0)),
                        -float(item["confidence"]),
                        -float(item["_joinability"]["joinability_score"]),
                        _hash_int(seed, ordinal, role, item["segment_id"]),
                    ),
                )
                for role, pool in pools.items()
            }
            best: tuple[tuple[Any, ...], dict[str, Any]] | None = None
            nodes_for_composition = 0
            for attempt in range(self.max_proposals):
                items: list[dict[str, Any]] = []
                for position, role in enumerate(roles):
                    nodes_for_composition += 1
                    partial_nodes += 1
                    if nodes_for_composition > MAX_PARTIAL_NODES_PER_COMPOSITION:
                        break
                    choice = self._proposal_choice(
                        ranked[role], items, seed, ordinal, attempt, position,
                    )
                    if choice is None:
                        break
                    items.append(choice)
                    remaining_roles = roles[position + 1:]
                    partial_duration = sum(float(item["duration_seconds"]) for item in items)
                    minimum_remaining = sum(
                        duration_bounds[remaining_role][0]
                        for remaining_role in remaining_roles
                    )
                    maximum_remaining = sum(
                        duration_bounds[remaining_role][1]
                        for remaining_role in remaining_roles
                    )
                    if (
                        partial_duration + minimum_remaining > target_max_duration
                        or partial_duration + maximum_remaining < target_min_duration
                    ):
                        break
                if len(items) != len(roles):
                    continue
                proposals_evaluated += 1
                total_duration = sum(float(item["duration_seconds"]) for item in items)
                if not target_min_duration <= total_duration <= target_max_duration:
                    duration_rejections += 1
                    continue
                signature = exact_signature(items)
                if signature in known_exact:
                    duplicate_rejections += 1
                    continue
                role_key = tuple(item["role"] for item in items)
                near_candidates = known_by_first_source.get((role_key, items[0]["source_id"]), ())
                if any(effectively_same_timeline(items, item["items"]) for item in near_candidates):
                    duplicate_rejections += 1
                    continue
                relevant: dict[str, dict[str, Any]] = {}
                for item in items:
                    relevant.update(known_by_source.get(item["source_id"], {}))
                maximum_similarity = max(
                    (timeline_similarity(items, item["items"]) for item in relevant.values()),
                    default=0.0,
                )
                composition = self._build_composition(
                    items=items,
                    ordinal=ordinal,
                    requested_template=requested_template,
                    actual_template=actual_template,
                    fallback_reason=fallback_reason,
                    cta_mode=cta_mode,
                    target_min_duration=target_min_duration,
                    target_max_duration=target_max_duration,
                    approved_usage=approved_usage,
                    current_usage=usage,
                    maximum_similarity=maximum_similarity,
                    proposals_evaluated=attempt + 1,
                )
                rank = (
                    composition["selection_score"],
                    -max(usage[item["segment_id"]] for item in items),
                    -sum(usage[item["segment_id"]] for item in items),
                    -_hash_int(seed, ordinal, signature),
                )
                if best is None or rank > best[0]:
                    best = (rank, composition)

            if best is None:
                break
            composition = best[1]
            generated.append(composition)
            known.append(composition)
            known_exact.add(composition["exact_signature"])
            self._index_comparison(composition, known_by_first_source, known_by_source)
            for item in composition["items"]:
                usage[item["segment_id"]] += 1

        warnings = self._reuse_warnings(pools, roles, requested_count)
        if len(generated) < requested_count:
            warnings.append({
                "code": "search_exhausted",
                "requested": requested_count,
                "generated": len(generated),
                "shortfall": requested_count - len(generated),
            })
        stats = self._statistics(proposals_evaluated, partial_nodes, duration_rejections, pools)
        stats["duplicate_rejections"] = duplicate_rejections
        stats["requested"] = requested_count
        stats["generated"] = len(generated)
        stats["joinability_inventory"] = quality_inventory
        selected = [item for composition in generated for item in composition["items"]]
        stats["selected_contextual_boundaries"] = sum(
            item.get("ranking_metadata", {}).get("joinability", {}).get("boundary_label") == "Contextual"
            for item in selected
        )
        stats["mean_hook_benefits_continuity"] = round(
            sum(composition["selection_metadata"]["hook_benefits_continuity"] for composition in generated)
            / len(generated), 4,
        ) if generated else 0.0
        return generated, warnings, stats

    @staticmethod
    def _index_comparison(
        composition: dict[str, Any],
        by_first_source: dict[tuple[tuple[str, ...], str], list[dict[str, Any]]],
        by_source: dict[str, dict[str, dict[str, Any]]],
    ) -> None:
        items = composition.get("items", ())
        if not items:
            return
        key = (tuple(item["role"] for item in items), items[0]["source_id"])
        by_first_source[key].append(composition)
        identity = str(composition.get("composition_id") or composition["exact_signature"])
        for source_id in {item["source_id"] for item in items}:
            by_source[source_id][identity] = composition

    def _candidate_pools(
        self,
        segments: Sequence[dict[str, Any]],
        roles: set[str],
        approved_usage: dict[str, dict[str, Any]],
        seed: str,
    ) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = {}
        for role in roles:
            candidates = [item for item in segments if item["role"] == role]
            if len(candidates) <= self.max_role_pool:
                result[role] = sorted(
                    candidates,
                    key=lambda item: (
                        int(approved_usage.get(item["segment_id"], {}).get("usage_count", 0)),
                        -float(item["confidence"]),
                        -float(item["_joinability"]["joinability_score"]), item["segment_id"],
                    ),
                )
                continue
            buckets: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
            for item in candidates:
                buckets[(item["source_id"], int(float(item["duration_seconds"]) // 10))].append(item)
            for values in buckets.values():
                values.sort(key=lambda item: (
                    int(approved_usage.get(item["segment_id"], {}).get("usage_count", 0)),
                    -float(item["confidence"]),
                    -float(item["_joinability"]["joinability_score"]), item["segment_id"],
                ))
            keys = sorted(buckets, key=lambda key: _hash_int(seed, role, *key))
            selected: list[dict[str, Any]] = []
            while len(selected) < self.max_role_pool and any(buckets[key] for key in keys):
                for key in keys:
                    if buckets[key] and len(selected) < self.max_role_pool:
                        selected.append(buckets[key].pop(0))
            result[role] = selected
        return result

    @staticmethod
    def _proposal_choice(
        pool: Sequence[dict[str, Any]],
        selected: Sequence[dict[str, Any]],
        seed: str,
        ordinal: int,
        attempt: int,
        position: int,
    ) -> dict[str, Any] | None:
        selected_ids = {item["segment_id"] for item in selected}
        window = min(len(pool), 16 + 16 * (attempt // 32))
        if window == 0:
            return None
        start = _hash_int(seed, ordinal, attempt, position) % window
        for shift in range(window):
            item = pool[(start + shift) % window]
            if item["segment_id"] not in selected_ids:
                return item
        return None

    @staticmethod
    def _build_composition(
        *,
        items: list[dict[str, Any]],
        ordinal: int,
        requested_template: str,
        actual_template: str,
        fallback_reason: str | None,
        cta_mode: str,
        target_min_duration: float,
        target_max_duration: float,
        approved_usage: dict[str, dict[str, Any]],
        current_usage: Counter[str],
        maximum_similarity: float,
        proposals_evaluated: int,
    ) -> dict[str, Any]:
        total = sum(float(item["duration_seconds"]) for item in items)
        midpoint = (target_min_duration + target_max_duration) / 2
        half_range = (target_max_duration - target_min_duration) / 2
        duration_fit = max(0.0, 1.0 - abs(total - midpoint) / half_range)
        distinct_sources = len({item["source_id"] for item in items})
        source_diversity = (distinct_sources - 1) / (len(items) - 1) if len(items) > 1 else 1.0
        confidence = sum(float(item["confidence"]) for item in items) / len(items)
        combined_usage = [
            current_usage[item["segment_id"]]
            + int(approved_usage.get(item["segment_id"], {}).get("usage_count", 0))
            for item in items
        ]
        usage_fairness = 1.0 / (1.0 + sum(combined_usage) / len(combined_usage))
        novelty = 1.0 - maximum_similarity
        joinability = sum(
            float(item["_joinability"]["joinability_score"]) for item in items
        ) / len(items)
        continuity = composition_continuity(items)
        score_components = {
            "duration_fit": 28 * duration_fit,
            "source_diversity": 25 * source_diversity,
            "mean_confidence": 20 * confidence,
            "usage_fairness": 18 * usage_fairness,
            "joinability": 6 * joinability,
            "hook_benefits_continuity": 2 * continuity,
            "novelty": 1 * novelty,
        }
        score = sum(score_components.values())
        selected_items: list[dict[str, Any]] = []
        for item in items:
            copy = dict(item)
            assessment = copy.pop("_joinability")
            copy["approved_usage_at_selection"] = int(
                approved_usage.get(item["segment_id"], {}).get("usage_count", 0)
            )
            copy["current_run_usage_at_selection"] = current_usage[item["segment_id"]]
            copy["ranking_metadata"] = {"joinability": assessment}
            selected_items.append(copy)
        return {
            "ordinal": ordinal,
            "requested_template": requested_template,
            "actual_template": actual_template,
            "fallback_reason": fallback_reason,
            "cta_mode": cta_mode,
            "target_min_duration": target_min_duration,
            "target_max_duration": target_max_duration,
            "actual_duration": total,
            "distinct_source_count": distinct_sources,
            "selection_score": score,
            "selection_metadata": {
                "duration_fit": duration_fit,
                "source_diversity": source_diversity,
                "mean_confidence": confidence,
                "usage_fairness": usage_fairness,
                "mean_joinability": joinability,
                "minimum_joinability": min(
                    float(item["_joinability"]["joinability_score"]) for item in items
                ),
                "hook_benefits_continuity": continuity,
                "novelty": novelty,
                "maximum_timeline_similarity": maximum_similarity,
                "proposals_until_candidate": proposals_evaluated,
                "score_components": score_components,
            },
            "exact_signature": exact_signature(selected_items),
            "near_signature": near_signature(selected_items),
            "signature_version": SIGNATURE_VERSION,
            "items": selected_items,
        }

    @staticmethod
    def _reuse_warnings(
        pools: dict[str, list[dict[str, Any]]], roles: Sequence[str], requested_count: int,
    ) -> list[dict[str, Any]]:
        requirements = Counter(roles)
        warnings = []
        for role, per_composition in requirements.items():
            required_uses = requested_count * per_composition
            available = len(pools.get(role, ()))
            if available and required_uses > available:
                warnings.append({
                    "code": "role_inventory_requires_reuse",
                    "role": role,
                    "available_segments": available,
                    "requested_uses": required_uses,
                })
        return warnings

    def _statistics(
        self,
        proposals: int,
        nodes: int,
        duration_rejections: int,
        pools: dict[str, list[dict[str, Any]]],
    ) -> dict[str, Any]:
        return {
            "candidate_pool_limit": self.max_role_pool,
            "proposal_limit_per_composition": self.max_proposals,
            "partial_node_limit_per_composition": MAX_PARTIAL_NODES_PER_COMPOSITION,
            "proposals_evaluated": proposals,
            "partial_nodes_evaluated": nodes,
            "duration_rejections": duration_rejections,
            "pool_sizes": {role: len(values) for role, values in pools.items()},
        }
