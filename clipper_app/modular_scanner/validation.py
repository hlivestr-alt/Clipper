from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from .constants import (
    DEDUPE_OVERLAP_THRESHOLD,
    MINIMUM_DURATION_SECONDS,
    MINIMUM_REPAIRABLE_DURATION_SECONDS,
    PRODUCT_ALIASES,
    PRODUCT_CONTEXT_SECONDS,
    PRODUCTS,
    ROLES,
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


def validate_candidate(
    candidate: Any,
    window: dict[str, Any],
    vod_duration: float,
    *,
    order: int = 0,
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
    if duration < MINIMUM_DURATION_SECONDS:
        if duration < MINIMUM_REPAIRABLE_DURATION_SECONDS:
            return None, Rejection(
                "duration_too_short",
                f"Duration repair not attempted: snapped duration {duration:.3f}s is below 10.000s",
            )
        repaired, repair_diagnostics = _repair_duration(
            window["segments"], covered, product, snapped_start, snapped_end,
        )
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
    if midpoint < window["ownership_start"] or (midpoint >= window["ownership_end"] and not final_window):
        return None, Rejection("overlap_ownership", "Neighboring window owns this candidate")
    text = " ".join(segment["text"].strip() for segment in covered if segment["text"].strip())
    evidence = product_evidence(text)
    declared_strength = evidence.get(product, 0)
    if declared_strength == 0:
        context_evidence = _nearby_product_evidence(window["segments"], snapped_start, snapped_end)
        declared_strength = context_evidence.get(product, 0)
        if declared_strength == 0:
            return None, Rejection("product_not_supported", "Range and bounded nearby context do not support the declared product")
        conflicts = [name for name, strength in context_evidence.items() if name != product and strength > 0]
        if conflicts:
            return None, Rejection("conflicting_product", "Nearby context contains conflicting product evidence")
    conflicts = [name for name, strength in evidence.items() if name != product and strength > 0]
    if conflicts:
        return None, Rejection("conflicting_product", "Transcript contains evidence for multiple products")
    if role == "cta" and _USAGE_TERMS.search(text) and not _CTA_TERMS.search(text):
        return None, Rejection("role_not_supported", "Usage/tutorial instructions without a purchase action are not CTA")
    return {
        "product": product,
        "role": role,
        "start_seconds": round(snapped_start, 3),
        "end_seconds": round(snapped_end, 3),
        "duration_seconds": round(duration, 3),
        "confidence": float(confidence),
        "transcript_text": text,
        "reason": reason.strip(),
        "validation_diagnostics": {"duration_repair": repair_diagnostics},
        "_product_evidence": declared_strength,
        "_coverage": coverage,
        "_order": order,
    }, None


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
    segments: list[dict[str, Any]], start: float, end: float,
) -> dict[str, int]:
    nearby: list[tuple[float, dict[str, int]]] = []
    for segment in segments:
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


def _quality_key(item: dict[str, Any]) -> tuple[float, int, float, int]:
    return (
        float(item["confidence"]),
        int(item.get("_product_evidence", 0)),
        float(item.get("_coverage", 0)),
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
