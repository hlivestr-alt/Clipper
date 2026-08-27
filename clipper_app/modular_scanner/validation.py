from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from .constants import (
    ACTIVE_PRODUCT_CONTEXT_SECONDS,
    COMPOSITION_GAP_CONFIDENCE_PENALTY_PER_SECOND,
    COMPOSITION_MAXIMUM_CHAIN_LENGTH,
    COMPOSITION_MAXIMUM_GAP_SECONDS,
    COMPOSITION_PREFERRED_MINIMUM_SECONDS,
    CROSS_WINDOW_PRODUCT_CONFLICT_IOU_THRESHOLD,
    DEDUPE_OVERLAP_THRESHOLD,
    MINIMUM_DURATION_SECONDS,
    MINIMUM_REPAIRABLE_DURATION_SECONDS,
    PRODUCT_ALIASES,
    PRODUCT_CONTEXT_SECONDS,
    PRODUCTS,
    ROLES,
)

ETALASE_PRODUCT_MAP = {
    1: "cleanser",
    2: "toner",
    3: "serum",
    4: "skin_cream",
    5: "eye_cream",
    6: "mask",
}
_ETALASE_REFERENCE = re.compile(
    r"\b(?:etalase|telasan)(?:\s+nomor)?\s*(?:ke\s*)?([1-6])\b",
    re.IGNORECASE,
)
_EXPLICIT_TRANSITION = re.compile(
    r"\b(?:sekarang\s+)?(?:kita\s+)?(?:lanjut|lanjutkan|pindah|masuk)\b|\bsekarang\s+(?:bahas|ke)\b",
    re.IGNORECASE,
)

_TOPIC_TERMS = re.compile(
    r"\b(?:kulit|wajah|muka|tekstur|lembap|lembut|cerah|kusam|jerawat|pori|noda|flek|"
    r"pakai|gunakan|aplikasi|oles|formula|kandungan|ingredient|hidrasi|glowing|bersih|bilas|"
    r"busa|foam|massage|pijat|mata|dark spot|anti aging|niacinamide|vitamin|retinol|acid)\b",
    re.IGNORECASE,
)
_UNRELATED_FILLER = re.compile(
    r"\b(?:terima kasih|sampai jumpa|halo guys|sebentar ya|musik|cuaca|ngobrol dulu)\b",
    re.IGNORECASE,
)
_CTA_TERMS = re.compile(
    r"\b(?:checkout|check out|beli|buy|keranjang kuning|yellow cart|etalase|promo|diskon|discount|"
    r"harga|limited price|buy\s*2\s*get\s*1|gratis|voucher|klik|order|pesan sekarang)\b",
    re.IGNORECASE,
)
_USAGE_TERMS = re.compile(
    r"\b(?:basahi|wet|telapak|palm|tambah(?:kan)? air|busa|foam|pijat|massage|bilas|rinse|"
    r"oles(?:kan)?|aplikasi(?:kan)?|30\s*[-–]\s*60 detik)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Rejection:
    code: str
    detail: str


@dataclass(frozen=True)
class ProductContextEvent:
    at: float
    product: str | None
    source: str
    segment_end: float


def build_product_context(segments: list[dict[str, Any]]) -> list[ProductContextEvent]:
    """Build a conservative product timeline from authoritative transcript segments.

    An etalase number is trusted only after that exact PROYA number/product pair is
    confirmed by explicit product evidence in the same transcript segment. Explicit
    evidence always wins and ambiguous/conflicting evidence terminates inheritance.
    """
    confirmed_etalase: set[int] = set()
    contradicted_etalase: set[int] = set()
    for segment in segments:
        evidence = product_evidence(str(segment.get("text") or ""))
        if len(evidence) != 1:
            continue
        explicit_product = next(iter(evidence))
        for number in _etalase_numbers(str(segment.get("text") or "")):
            if ETALASE_PRODUCT_MAP[number] == explicit_product:
                confirmed_etalase.add(number)
            else:
                contradicted_etalase.add(number)
    confirmed_etalase.difference_update(contradicted_etalase)

    events: list[ProductContextEvent] = []
    for segment in sorted(segments, key=lambda item: (float(item["start"]), float(item["end"]))):
        text = str(segment.get("text") or "")
        evidence = product_evidence(text)
        numbers = _etalase_numbers(text)
        if len(evidence) == 1:
            source = "explicit_transition" if numbers or _EXPLICIT_TRANSITION.search(text) else "explicit"
            events.append(ProductContextEvent(
                float(segment["start"]), next(iter(evidence)), source, float(segment["end"]),
            ))
        elif len(evidence) > 1:
            events.append(ProductContextEvent(float(segment["start"]), None, "ambiguous", float(segment["end"])))
        elif numbers:
            inferred = {ETALASE_PRODUCT_MAP[number] for number in numbers if number in confirmed_etalase}
            if len(inferred) == 1:
                events.append(ProductContextEvent(
                    float(segment["start"]), next(iter(inferred)), "etalase", float(segment["end"]),
                ))
            elif len(inferred) > 1:
                events.append(ProductContextEvent(float(segment["start"]), None, "ambiguous", float(segment["end"])))
        elif _UNRELATED_FILLER.search(text):
            events.append(ProductContextEvent(float(segment["start"]), None, "topic_boundary", float(segment["end"])))
    return events


def validate_candidate(
    candidate: Any,
    window: dict[str, Any],
    vod_duration: float,
    *,
    order: int = 0,
    product_context: list[ProductContextEvent] | None = None,
    allow_short: bool = False,
    attempt_duration_repair: bool = True,
    enforce_ownership: bool = True,
    defer_product_validation: bool = False,
) -> tuple[dict[str, Any] | None, Rejection | None]:
    required = {"start_seconds", "end_seconds", "product", "role", "confidence", "reason"}
    if not isinstance(candidate, dict) or not required.issubset(candidate):
        return None, Rejection("invalid_contract", "Candidate is missing required fields")
    product = candidate.get("product")
    if product not in PRODUCTS:
        return None, Rejection("invalid_product", "Product is not an exact scanner enum")
    role = candidate.get("role")
    if role not in ROLES:
        return None, Rejection("invalid_role", "Role is not an exact scanner enum")
    start = _finite_number(candidate.get("start_seconds"))
    end = _finite_number(candidate.get("end_seconds"))
    confidence = _finite_number(candidate.get("confidence"))
    if start is None or end is None:
        return None, Rejection("invalid_timestamp", "Timestamps must be finite numbers")
    if confidence is None or not 0 <= confidence <= 1:
        return None, Rejection("invalid_confidence", "Confidence must be between 0 and 1")
    if start < 0 or end <= start or end > vod_duration:
        return None, Rejection("source_bounds", "Candidate is outside VOD bounds")
    if start < window["start"] or end > window["end"]:
        return None, Rejection("window_bounds", "Candidate is outside the analyzed window")
    reason = candidate.get("reason")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
        return None, Rejection("invalid_reason", "Reason must be a non-empty bounded string")

    covered = [segment for segment in window["segments"] if segment["end"] > start and segment["start"] < end]
    if not covered:
        return None, Rejection("no_transcript_coverage", "No authoritative transcript covers the range")
    overlap_duration = sum(max(0.0, min(end, segment["end"]) - max(start, segment["start"])) for segment in covered)
    coverage = min(1.0, overlap_duration / (end - start))
    if coverage < 0.25:
        return None, Rejection("insufficient_transcript_coverage", "Authoritative transcript coverage is too sparse")
    snapped_start = covered[0]["start"] if abs(covered[0]["start"] - start) <= 1.5 else start
    snapped_end = covered[-1]["end"] if abs(covered[-1]["end"] - end) <= 1.5 else end
    duration = snapped_end - snapped_start
    repair_diagnostics: dict[str, Any] = {"attempted": False, "outcome": "not_needed"}
    if duration < MINIMUM_DURATION_SECONDS and not allow_short:
        if duration < MINIMUM_REPAIRABLE_DURATION_SECONDS:
            return None, Rejection(
                "duration_too_short",
                f"Duration repair not attempted: snapped duration {duration:.3f}s is below 10.000s",
            )
        repaired, repair_diagnostics = _repair_duration(
            window["segments"], covered, product, snapped_start, snapped_end,
        ) if attempt_duration_repair else (None, {
            "attempted": False, "outcome": "deferred", "reason": "duration repair deferred for composition",
        })
        if repaired is None:
            return None, Rejection("duration_too_short", f"Duration repair failed: {repair_diagnostics['reason']}")
        snapped_start, snapped_end = repaired
        duration = snapped_end - snapped_start
        if snapped_end > vod_duration:
            return None, Rejection("source_bounds", "Duration repair would exceed VOD bounds")
        covered = [
            segment for segment in window["segments"]
            if segment["end"] > snapped_start and segment["start"] < snapped_end
        ]
        overlap_duration = sum(
            max(0.0, min(snapped_end, segment["end"]) - max(snapped_start, segment["start"]))
            for segment in covered
        )
        coverage = min(1.0, overlap_duration / duration)

    midpoint = (snapped_start + snapped_end) / 2.0
    final_window = math.isclose(window["ownership_end"], window["end"], abs_tol=1e-6)
    if enforce_ownership and (
        midpoint < window["ownership_start"] or (midpoint >= window["ownership_end"] and not final_window)
    ):
        return None, Rejection("overlap_ownership", "Neighboring window owns this candidate")
    text = " ".join(segment["text"].strip() for segment in covered if segment["text"].strip())
    if defer_product_validation:
        if role == "cta" and _USAGE_TERMS.search(text) and not _CTA_TERMS.search(text):
            return None, Rejection("role_not_supported", "Usage/tutorial instructions without a purchase action are not CTA")
        return _validated_result(
            product, role, snapped_start, snapped_end, duration, confidence, text, reason,
            repair_diagnostics, "deferred", 0, coverage, order,
        ), None
    evidence = product_evidence(text)
    local_declared_strength = evidence.get(product, 0)
    declared_strength = local_declared_strength
    evidence_source = "local"
    local_conflicts = [name for name, strength in evidence.items() if name != product and strength > 0]
    if local_conflicts:
        return None, Rejection("conflicting_product", "Explicit local transcript evidence conflicts with the declared product")
    if declared_strength == 0:
        latest = _latest_context_event_at(product_context or [], snapped_start)
        active = _active_context_at(product_context or [], snapped_start)
        if active is not None and active.product == product and active.source in {"explicit", "explicit_transition"}:
            declared_strength = 1
            evidence_source = "active_context"
        elif _is_immediate_confirmed_transition(active, snapped_start, product):
            return None, Rejection("conflicting_product", "A confirmed immediate product transition conflicts with the declared product")
        if declared_strength == 0:
            boundary = latest.at if latest is not None and latest.product is None else None
            context_evidence = _nearby_product_evidence(
                window["segments"], snapped_start, snapped_end, lower_bound=boundary,
            )
            declared_strength = context_evidence.get(product, 0)
            if declared_strength:
                conflicts = [name for name, strength in context_evidence.items() if name != product and strength > 0]
                if conflicts:
                    return None, Rejection("conflicting_product", "Nearby context contains conflicting product evidence")
                evidence_source = "nearby"
        if declared_strength == 0 and active is not None and active.source == "etalase" and active.product == product:
            declared_strength = 1
            evidence_source = "etalase_context"
        if declared_strength == 0:
            return None, Rejection("product_not_supported", "Range, active context, and bounded nearby context do not support the declared product")
    if local_declared_strength == 0 and _context_transition_between(
        product_context or [], snapped_start, snapped_end, product, confirmed_only=True,
    ):
        return None, Rejection("conflicting_product", "A different product transition occurs inside the candidate range")
    if role == "cta" and _USAGE_TERMS.search(text) and not _CTA_TERMS.search(text):
        return None, Rejection("role_not_supported", "Usage/tutorial instructions without a purchase action are not CTA")
    return _validated_result(
        product, role, snapped_start, snapped_end, duration, confidence, text, reason,
        repair_diagnostics, evidence_source, declared_strength, coverage, order,
    ), None


def _validated_result(
    product: str,
    role: str,
    start: float,
    end: float,
    duration: float,
    confidence: float,
    text: str,
    reason: str,
    repair_diagnostics: dict[str, Any],
    evidence_source: str,
    declared_strength: int,
    coverage: float,
    order: int,
) -> dict[str, Any]:
    return {
        "product": product,
        "role": role,
        "start_seconds": round(start, 3),
        "end_seconds": round(end, 3),
        "duration_seconds": round(duration, 3),
        "confidence": float(confidence),
        "transcript_text": text,
        "reason": reason.strip(),
        "validation_diagnostics": {
            "duration_repair": repair_diagnostics,
            "product_evidence": {"source": evidence_source},
        },
        "_product_evidence": declared_strength,
        "_coverage": coverage,
        "_order": order,
    }


def resolve_cross_window_product_conflicts(
    candidates: list[dict[str, Any]],
    transcript_segments: list[dict[str, Any]],
    product_context: list[ProductContextEvent],
    threshold: float = CROSS_WINDOW_PRODUCT_CONFLICT_IOU_THRESHOLD,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Resolve same-passage product disagreement before overlap ownership filtering."""
    adjacency: dict[int, set[int]] = {index: set() for index in range(len(candidates))}
    edge_iou: dict[tuple[int, int], float] = {}
    for left_index, left in enumerate(candidates):
        for right_index in range(left_index + 1, len(candidates)):
            right = candidates[right_index]
            if left.get("_chunk_index") == right.get("_chunk_index"):
                continue
            if left["role"] != right["role"] or left["product"] == right["product"]:
                continue
            iou = _temporal_iou(left, right)
            if iou < threshold:
                continue
            adjacency[left_index].add(right_index)
            adjacency[right_index].add(left_index)
            edge_iou[(left_index, right_index)] = iou

    removed: set[int] = set()
    diagnostics: list[dict[str, Any]] = []
    visited: set[int] = set()
    for seed, neighbors in adjacency.items():
        if seed in visited or not neighbors:
            continue
        component: set[int] = set()
        stack = [seed]
        while stack:
            index = stack.pop()
            if index in component:
                continue
            component.add(index)
            stack.extend(adjacency[index] - component)
        visited.update(component)
        component_edges = [
            (pair, iou) for pair, iou in edge_iou.items()
            if pair[0] in component and pair[1] in component
        ]
        strongest_pair, strongest_iou = max(component_edges, key=lambda item: item[1])
        left, right = (candidates[index] for index in strongest_pair)
        overlap_start = max(left["start_seconds"], right["start_seconds"])
        overlap_end = min(left["end_seconds"], right["end_seconds"])
        products = {candidates[index]["product"] for index in component}
        winner, evidence = _resolve_conflicting_product(
            products, overlap_start, overlap_end, transcript_segments, product_context,
        )
        if winner is None:
            discarded = set(component)
            resolution = "rejected_ambiguous"
        else:
            discarded = {index for index in component if candidates[index]["product"] != winner}
            resolution = f"{winner}_kept"
            for index in component - discarded:
                candidates[index]["_cross_window_resolution_winner"] = True
        removed.update(discarded)
        diagnostic = {
            "status": "cross_window_product_conflict" if winner is None else "cross_window_product_conflict_resolved",
            "candidates": [_cross_window_candidate_details(candidates[index]) for index in sorted(component)],
            "competing_products": sorted(products),
            "overlap_start_seconds": round(overlap_start, 3),
            "overlap_end_seconds": round(overlap_end, 3),
            "temporal_iou": round(strongest_iou, 6),
            "threshold": threshold,
            "evidence": evidence,
            "resolution": resolution,
            "discarded_candidate_count": len(discarded),
        }
        diagnostics.append(diagnostic)
        if winner is not None:
            for index in component - discarded:
                candidates[index].setdefault("validation_diagnostics", {})["cross_window_product_conflict"] = diagnostic
    return [candidate for index, candidate in enumerate(candidates) if index not in removed], diagnostics


def _resolve_conflicting_product(
    products: set[str],
    overlap_start: float,
    overlap_end: float,
    segments: list[dict[str, Any]],
    events: list[ProductContextEvent],
) -> tuple[str | None, str]:
    overlap_text = " ".join(
        str(segment.get("text") or "").strip()
        for segment in segments
        if segment["end"] > overlap_start and segment["start"] < overlap_end and str(segment.get("text") or "").strip()
    )
    all_explicit = set(product_evidence(overlap_text))
    explicit = all_explicit & products
    if len(all_explicit) == 1 and len(explicit) == 1:
        return next(iter(explicit)), "explicit_product_in_overlap"
    if len(all_explicit) > 1:
        return None, "multiple_explicit_products_in_overlap"

    latest = _latest_context_event_at(events, overlap_start)
    if latest is not None and latest.product in products:
        age = overlap_start - latest.segment_end
        if 0 <= age <= PRODUCT_CONTEXT_SECONDS and latest.source == "explicit_transition":
            return latest.product, "confirmed_explicit_transition"
        if 0 <= age <= PRODUCT_CONTEXT_SECONDS and latest.source == "explicit":
            return latest.product, "fresh_active_product_context"
        if 0 <= age <= PRODUCT_CONTEXT_SECONDS and latest.source == "etalase":
            return latest.product, "confirmed_etalase_context"
    return None, "no_decisive_authoritative_product_evidence"


def _cross_window_candidate_details(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "chunk_index": int(candidate.get("_chunk_index", -1)),
        "ordinal": int(candidate.get("_order", -1)),
        "product": candidate["product"],
        "role": candidate["role"],
        "start_seconds": candidate["start_seconds"],
        "end_seconds": candidate["end_seconds"],
        "composed": bool((candidate.get("validation_diagnostics") or {}).get("composition")),
    }


def _temporal_iou(left: dict[str, Any], right: dict[str, Any]) -> float:
    intersection = max(
        0.0,
        min(float(left["end_seconds"]), float(right["end_seconds"]))
        - max(float(left["start_seconds"]), float(right["start_seconds"])),
    )
    union = max(float(left["end_seconds"]), float(right["end_seconds"])) - min(
        float(left["start_seconds"]), float(right["start_seconds"]),
    )
    return intersection / union if union > 0 else 0.0


def compose_candidates(
    candidates: list[dict[str, Any]],
    window: dict[str, Any],
    product_context: list[ProductContextEvent] | None = None,
    repair_options: dict[int, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Compose the smallest valid adjacent same-product/role chains.

    Confidence is duration-weighted across source candidates, minus 0.005 per
    second of positive inter-candidate gap, clamped to [0, 1].
    """
    ordered = sorted(candidates, key=lambda item: (item["start_seconds"], item.get("_order", 0)))
    consumed: set[int] = set()
    composed: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    for index, first in enumerate(ordered):
        if index in consumed or first["duration_seconds"] >= MINIMUM_DURATION_SECONDS:
            continue
        chain = [first]
        chain_indexes = [index]
        for next_index in range(index + 1, min(len(ordered), index + COMPOSITION_MAXIMUM_CHAIN_LENGTH)):
            neighbor = ordered[next_index]
            if next_index in consumed:
                break
            if neighbor["duration_seconds"] >= MINIMUM_DURATION_SECONDS:
                break
            if neighbor["product"] != first["product"] or neighbor["role"] != first["role"]:
                break
            gap = neighbor["start_seconds"] - chain[-1]["end_seconds"]
            if gap < 0 or gap > COMPOSITION_MAXIMUM_GAP_SECONDS:
                break
            if _composition_interrupted(
                window["segments"], chain[-1]["end_seconds"], neighbor["start_seconds"], first["product"],
                product_context or [],
            ):
                break
            chain.append(neighbor)
            chain_indexes.append(next_index)
            if not _authoritative_range_covered(
                window["segments"], chain[0]["start_seconds"], chain[-1]["end_seconds"],
            ):
                break
            combined_duration = chain[-1]["end_seconds"] - chain[0]["start_seconds"]
            meaningful = any(item["duration_seconds"] >= COMPOSITION_PREFERRED_MINIMUM_SECONDS for item in chain)
            if combined_duration >= MINIMUM_DURATION_SECONDS and meaningful:
                result = _build_composed_candidate(chain, window)
                repair = (repair_options or {}).get(int(first.get("_order", -1)))
                if repair is not None and repair["duration_seconds"] < result["duration_seconds"]:
                    break
                composed.append(result)
                consumed.update(chain_indexes)
                diagnostics.extend(_composition_diagnostics(chain, result))
                break
    remaining = [item for index, item in enumerate(ordered) if index not in consumed]
    return remaining + composed, diagnostics


def _etalase_numbers(text: str) -> list[int]:
    return [int(match.group(1)) for match in _ETALASE_REFERENCE.finditer(text)]


def _active_context_at(events: list[ProductContextEvent], at: float) -> ProductContextEvent | None:
    active = _latest_context_event_at(events, at)
    if active is None:
        return None
    if active.product is None or at - active.segment_end > ACTIVE_PRODUCT_CONTEXT_SECONDS:
        return None
    return active


def _latest_context_event_at(events: list[ProductContextEvent], at: float) -> ProductContextEvent | None:
    previous = [event for event in events if event.at <= at]
    return previous[-1] if previous else None


def _is_immediate_confirmed_transition(
    event: ProductContextEvent | None, at: float, product: str,
) -> bool:
    return bool(
        event is not None
        and event.product not in {None, product}
        and event.source in {"explicit_transition", "etalase"}
        and 0 <= at - event.segment_end <= PRODUCT_CONTEXT_SECONDS
    )


def _context_transition_between(
    events: list[ProductContextEvent], start: float, end: float, product: str, *, confirmed_only: bool = False,
) -> bool:
    return any(
        event.at > start
        and event.at < end
        and event.product != product
        and (not confirmed_only or event.source in {"explicit_transition", "etalase", "ambiguous"})
        for event in events
    )


def _composition_interrupted(
    segments: list[dict[str, Any]], start: float, end: float, product: str,
    events: list[ProductContextEvent],
) -> bool:
    if _context_transition_between(events, start, end, product, confirmed_only=True):
        return True
    between = [segment for segment in segments if segment["end"] > start and segment["start"] < end]
    if not between:
        return False
    for segment in between:
        text = str(segment.get("text") or "")
        evidence = product_evidence(text)
        if any(name != product for name in evidence) or _UNRELATED_FILLER.search(text):
            return True
    return False


def _authoritative_range_covered(
    segments: list[dict[str, Any]], start: float, end: float,
) -> bool:
    intervals = sorted(
        (max(start, float(item["start"])), min(end, float(item["end"])))
        for item in segments if item["end"] > start and item["start"] < end
    )
    if not intervals:
        return False
    covered = 0.0
    cursor = start
    maximum_hole = 0.0
    for left, right in intervals:
        maximum_hole = max(maximum_hole, max(0.0, left - cursor))
        if right > cursor:
            covered += right - max(left, cursor)
            cursor = right
    maximum_hole = max(maximum_hole, max(0.0, end - cursor))
    return covered / (end - start) >= 0.5 and maximum_hole <= COMPOSITION_MAXIMUM_GAP_SECONDS


def _build_composed_candidate(chain: list[dict[str, Any]], window: dict[str, Any]) -> dict[str, Any]:
    start = float(chain[0]["start_seconds"])
    end = float(chain[-1]["end_seconds"])
    covered = [segment for segment in window["segments"] if segment["end"] > start and segment["start"] < end]
    text = " ".join(str(segment["text"]).strip() for segment in covered if str(segment["text"]).strip())
    source_duration = sum(float(item["duration_seconds"]) for item in chain)
    weighted = sum(float(item["confidence"]) * float(item["duration_seconds"]) for item in chain) / source_duration
    total_gap = sum(
        max(0.0, float(right["start_seconds"]) - float(left["end_seconds"]))
        for left, right in zip(chain, chain[1:])
    )
    confidence = max(0.0, min(1.0, weighted - total_gap * COMPOSITION_GAP_CONFIDENCE_PENALTY_PER_SECOND))
    role_label = str(chain[0]["role"]).upper() if chain[0]["role"] == "cta" else str(chain[0]["role"]).title()
    reason = f"Combined adjacent {role_label} candidates into one coherent reusable thought."
    source_details = [
        {
            "ordinal": int(item.get("_order", 0)),
            "start_seconds": item["start_seconds"],
            "end_seconds": item["end_seconds"],
            "duration_seconds": item["duration_seconds"],
            "confidence": item["confidence"],
        }
        for item in chain
    ]
    coverage_duration = sum(
        max(0.0, min(end, segment["end"]) - max(start, segment["start"])) for segment in covered
    )
    return {
        "product": chain[0]["product"],
        "role": chain[0]["role"],
        "start_seconds": round(start, 3),
        "end_seconds": round(end, 3),
        "duration_seconds": round(end - start, 3),
        "confidence": round(confidence, 6),
        "transcript_text": text,
        "reason": reason,
        "validation_diagnostics": {
            "duration_repair": {"attempted": False, "outcome": "not_needed"},
            "composition": {
                "outcome": "composed",
                "reason": "smallest coherent adjacent same-product and same-role chain reaching 15.000s",
                "source_candidates": source_details,
                "total_gap_seconds": round(total_gap, 3),
                "confidence_formula": "duration_weighted_average - (0.005 * positive_gap_seconds)",
                "final_confidence": round(confidence, 6),
            },
        },
        "_product_evidence": sum(int(item.get("_product_evidence", 0)) for item in chain),
        "_coverage": min(1.0, coverage_duration / (end - start)),
        "_order": min(int(item.get("_order", 0)) for item in chain),
        "_chunk_index": int(chain[0].get("_chunk_index", -1)),
    }


def _composition_diagnostics(
    chain: list[dict[str, Any]], result: dict[str, Any],
) -> list[dict[str, Any]]:
    source_ordinals = [int(item.get("_order", 0)) for item in chain]
    return [
        {
            "status": "composed_into_segment",
            "source_ordinal": int(item.get("_order", 0)),
            "source_ordinals": source_ordinals,
            "source_duration_seconds": item["duration_seconds"],
            "source_confidence": item["confidence"],
            "composed_start_seconds": result["start_seconds"],
            "composed_end_seconds": result["end_seconds"],
            "composed_confidence": result["confidence"],
            "reason": "adjacent same-product and same-role candidates formed the smallest coherent valid range",
        }
        for item in chain
    ]


def product_evidence(text: str) -> dict[str, int]:
    normalized = _normalize_evidence_text(text)
    eye_cream_present = any(
        re.search(rf"(?<!\w){re.escape(_normalize_evidence_text(alias))}(?:\s*-?\s*nya)?(?!\w)", normalized)
        for alias in PRODUCT_ALIASES["eye_cream"]
    )
    evidence: dict[str, int] = {}
    for product, aliases in PRODUCT_ALIASES.items():
        hits = 0
        for alias in aliases:
            normalized_alias = _normalize_evidence_text(alias)
            if product == "skin_cream" and normalized_alias in {"cream", "krim"} and eye_cream_present:
                continue
            hits += len(re.findall(rf"(?<!\w){re.escape(normalized_alias)}(?:\s*-?\s*nya)?(?!\w)", normalized))
        if hits:
            evidence[product] = hits
    return evidence


def _normalize_evidence_text(text: str) -> str:
    """Normalize transcript evidence only; never use this for the raw LLM enum."""
    normalized = text.casefold().replace("’", "'")
    normalized = re.sub(r"[_/]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


def _nearby_product_evidence(
    segments: list[dict[str, Any]], start: float, end: float, *, lower_bound: float | None = None,
) -> dict[str, int]:
    nearby: list[tuple[float, dict[str, int]]] = []
    for segment in segments:
        if lower_bound is not None and segment["start"] < lower_bound:
            continue
        if segment["end"] <= start:
            distance = start - segment["end"]
        elif segment["start"] >= end:
            distance = segment["start"] - end
        else:
            continue
        if distance <= PRODUCT_CONTEXT_SECONDS:
            evidence = product_evidence(segment["text"])
            if evidence:
                nearby.append((distance, evidence))
    if not nearby:
        return {}
    closest = min(distance for distance, _ in nearby)
    result: dict[str, int] = {}
    for distance, evidence in nearby:
        if distance > closest + 3.0:
            continue
        for product, strength in evidence.items():
            result[product] = result.get(product, 0) + strength
    return result


def _repair_duration(
    segments: list[dict[str, Any]],
    covered: list[dict[str, Any]],
    product: str,
    original_start: float,
    original_end: float,
) -> tuple[tuple[float, float] | None, dict[str, Any]]:
    first = segments.index(covered[0])
    last = segments.index(covered[-1])
    options: list[tuple[tuple[float, bool, int, float], float, float]] = []
    for left in range(first, -1, -1):
        for right in range(last, len(segments)):
            if left == first and right == last:
                continue
            repaired_start = segments[left]["start"] if left < first else original_start
            repaired_end = segments[right]["end"] if right > last else original_end
            repaired_duration = repaired_end - repaired_start
            if repaired_duration < MINIMUM_DURATION_SECONDS:
                continue
            additions = [*segments[left:first], *segments[last + 1:right + 1]]
            if not all(_coherent_extension(segment["text"], product) for segment in additions):
                continue
            combined_text = " ".join(segment["text"] for segment in segments[left:right + 1])
            evidence = product_evidence(combined_text)
            if evidence.get(product, 0) == 0 or any(name != product for name in evidence):
                continue
            left_was_added = left < first
            key = (repaired_duration, left_was_added, len(additions), repaired_start)
            options.append((key, repaired_start, repaired_end))
    if not options:
        return None, {"attempted": True, "outcome": "failed", "reason": "no coherent same-product authoritative-boundary expansion reached 15.000s"}
    _, repaired_start, repaired_end = min(options, key=lambda item: item[0])
    return (repaired_start, repaired_end), {
        "attempted": True,
        "outcome": "expanded",
        "reason": "smallest coherent authoritative-boundary expansion",
        "original_start": round(original_start, 3),
        "original_end": round(original_end, 3),
        "repaired_start": round(repaired_start, 3),
        "repaired_end": round(repaired_end, 3),
    }


def _coherent_extension(text: str, product: str) -> bool:
    evidence = product_evidence(text)
    if any(name != product for name in evidence):
        return False
    if evidence.get(product, 0):
        return True
    if _UNRELATED_FILLER.search(text):
        return False
    return bool(_TOPIC_TERMS.search(text))


def deduplicate(segments: list[dict[str, Any]], threshold: float = DEDUPE_OVERLAP_THRESHOLD) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    for candidate in segments:
        duplicate_indexes = [
            index for index, existing in enumerate(kept)
            if existing["product"] == candidate["product"]
            and existing["role"] == candidate["role"]
            and _overlap_ratio(existing, candidate) >= threshold
        ]
        if not duplicate_indexes:
            kept.append(candidate)
            continue
        group = [candidate, *(kept[index] for index in duplicate_indexes)]
        winner = max(group, key=_quality_key)
        for index in reversed(duplicate_indexes):
            kept.pop(index)
        kept.append(winner)
    kept.sort(key=lambda item: (item["start_seconds"], item.get("_order", 0)))
    return [{key: value for key, value in item.items() if not key.startswith("_")} for item in kept]


def _quality_key(item: dict[str, Any]) -> tuple[float, int, int, float, float, int]:
    return (
        float(item["confidence"]),
        int(item.get("_product_evidence", 0)),
        int((item.get("validation_diagnostics") or {}).get("composition", {}).get("outcome") == "composed"),
        float(item.get("_coverage", 0)),
        -float(item.get("duration_seconds", 0)),
        -int(item.get("_order", 0)),
    )


def _overlap_ratio(left: dict[str, Any], right: dict[str, Any]) -> float:
    overlap = max(0.0, min(left["end_seconds"], right["end_seconds"]) - max(left["start_seconds"], right["start_seconds"]))
    shorter = min(left["duration_seconds"], right["duration_seconds"])
    return overlap / shorter if shorter > 0 else 0.0


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None
