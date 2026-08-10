from __future__ import annotations

import copy
import hashlib
import re
from typing import Any

from content_topics import timed_information_topic_windows
from product_broll import resolve_moment_product_key
from product_information import facts_for_product, load_product_information_index


PLAN_SCHEMA_VERSION = 7
DYNAMIC_TEXT_MODES = ("off", "minimal", "balanced", "high_energy")
DYNAMIC_TEXT_ROLES = ("ingredients", "benefits", "usage", "cta")
DEFAULT_DYNAMIC_TEXT_ROLES = DYNAMIC_TEXT_ROLES
MIN_DYNAMIC_CLIP_SECONDS = 5.0
SPEECH_TOPIC_GUARD_SECONDS = 0.35
MIN_INFORMATION_DISPLAY_SECONDS = 1.25
DYNAMIC_TEXT_HOLD_EXTENSION_SECONDS = 2.0

_MODE_BLOCK_LIMITS = {
    "minimal": (1, 2),
    "balanced": (2, 4),
    "high_energy": (4, 6),
}
_ROLE_TO_FACT_ROLE = {
    "ingredients": "ingredient",
    "benefits": "benefit",
    "usage": "usage",
    "cta": "cta",
}
_SECTION_LABELS = {
    "ingredients": "KANDUNGAN UTAMA:",
    "benefits": "FUNGSI PRODUK:",
    "usage": "CARA PAKAI:",
    "cta": "WORTH DICOBA?",
}
def build_dynamic_text_plan(
    moment: dict[str, Any],
    clip_words: list[dict[str, Any]],
    product_events: list[dict[str, Any]],
    clip_duration: float,
    cfg,
    *,
    variant=None,
) -> dict[str, Any]:
    mode = _dynamic_mode(cfg, variant)
    enabled_roles = _dynamic_roles(cfg, variant)
    settings = _dynamic_settings(cfg, variant)
    duration = max(0.0, float(clip_duration or 0.0))
    product_key = resolve_moment_product_key(moment, product_events=product_events)
    index = load_product_information_index(cfg)
    base = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "clip_id": str(moment.get("clip_id") or ""),
        "mode": mode,
        "enabled_roles": list(enabled_roles),
        "settings": copy.deepcopy(settings),
        "product_key": product_key or "",
        "product_information_revision": str(index.get("revision") or ""),
        "eligible": bool(mode != "off" and duration >= MIN_DYNAMIC_CLIP_SECONDS),
        "items": [],
        "dropped": [],
    }
    if not base["eligible"]:
        base["skip_reason"] = "disabled" if mode == "off" else "clip_too_short"
        return base

    hook_duration = _hook_duration(cfg, variant, duration)
    speech_matches, speech_dropped = _speech_topic_matches(
        clip_words,
        enabled_roles,
        hook_duration=hook_duration,
    )
    base["dropped"].extend(speech_dropped)
    candidates, candidate_dropped = _document_candidates(
        index,
        product_key,
        enabled_roles,
        moment,
        variant,
        speech_matches=speech_matches,
    )
    base["dropped"].extend(candidate_dropped)
    candidates = _dedupe_candidates(candidates)

    minimum, maximum = _MODE_BLOCK_LIMITS.get(mode, _MODE_BLOCK_LIMITS["balanced"])
    target = _target_count(duration, minimum, maximum)
    selected, selection_dropped = _select_candidates(candidates, target)
    base["dropped"].extend(selection_dropped)

    scheduled_items, scheduling_dropped = _schedule_items(
        selected,
        duration,
        hook_duration=hook_duration,
        settings=settings,
    )
    base["dropped"].extend(scheduling_dropped)
    base["items"] = resolve_dynamic_text_layout(
        scheduled_items,
        clip_id=base["clip_id"],
        variant_index=int(getattr(variant, "variant_index", getattr(cfg, "_variant_index", 0)) or 0),
        subtitle_position=str(
            getattr(variant, "subtitle_position", getattr(cfg, "_variant_subtitle_position", "bottom"))
            or "bottom"
        ),
        letterbox_top_frac=float(
            getattr(variant, "letterbox_top_frac", getattr(cfg, "_letterbox_top_frac", 0.0))
            or 0.0
        ),
        letterbox_bottom_frac=float(
            getattr(variant, "letterbox_bottom_frac", getattr(cfg, "_letterbox_bottom_frac", 0.0))
            or 0.0
        ),
    )
    base["source_counts"] = _source_counts(base["items"])
    return base


def dynamic_text_typography(
    frame_height: int,
    role: str,
    font_scale: float = 1.0,
    font_size: int | float | None = None,
) -> dict[str, int]:
    """Return shared box-free dynamic text sizes for preview and production."""
    scale = max(0.20, float(frame_height) / 1920.0)
    safe_font_scale = max(0.82, min(1.0, float(font_scale or 1.0)))
    normalized_role = str(role or "")
    configured_size = float(font_size) if font_size is not None else None
    body_base = configured_size if configured_size is not None and normalized_role != "closing_cta" else 35
    headline_base = (
        configured_size
        if configured_size is not None and normalized_role == "closing_cta"
        else body_base * (48.0 / 35.0)
    )
    headline_min = 16 if str(role or "") == "closing_cta" else 15
    return {
        "headline": max(headline_min, int(headline_base * scale * safe_font_scale)),
        "body": max(
            10 if normalized_role == "usage_step" else 11,
            int(body_base * scale * safe_font_scale),
        ),
    }


def dynamic_plan_text(plan: dict[str, Any]) -> str:
    parts = []
    for item in plan.get("items", []) if isinstance(plan, dict) else []:
        if not isinstance(item, dict):
            continue
        for key in ("headline", "text"):
            value = str(item.get(key) or "").strip()
            if value:
                parts.append(value)
        parts.extend(str(line).strip() for line in item.get("lines", []) or [] if str(line).strip())
    return "\n".join(parts)


def resolve_dynamic_text_layout(
    items: list[dict[str, Any]],
    *,
    clip_id: str = "",
    variant_index: int = 0,
    subtitle_position: str = "bottom",
    letterbox_top_frac: float = 0.0,
    letterbox_bottom_frac: float = 0.0,
) -> list[dict[str, Any]]:
    """Attach stable, frame-independent side placement to dynamic text items."""
    normalized_subtitle = str(subtitle_position or "bottom").strip().casefold()
    allowed_bands = {
        "top": ["middle", "lower"],
        "center": ["upper", "lower"],
        "bottom": ["upper", "middle"],
    }.get(normalized_subtitle, ["upper", "middle"])
    if float(letterbox_top_frac or 0.0) >= 0.22:
        allowed_bands = [band for band in allowed_bands if band != "upper"] or ["middle"]
    if float(letterbox_bottom_frac or 0.0) >= 0.22:
        allowed_bands = [band for band in allowed_bands if band != "lower"] or ["middle"]

    seed_raw = f"{clip_id}|{int(variant_index)}|dynamic-layout-v2"
    seed = int(hashlib.sha256(seed_raw.encode("utf-8")).hexdigest()[:12], 16)
    first_side_index = seed % 2
    first_band_index = (seed // 2) % len(allowed_bands)
    output: list[dict[str, Any]] = []

    for item_index, source_item in enumerate(items or []):
        if not isinstance(source_item, dict):
            continue
        item = copy.deepcopy(source_item)
        existing = item.get("layout") if isinstance(item.get("layout"), dict) else {}
        side = str(existing.get("side") or ("left" if (first_side_index + item_index) % 2 == 0 else "right"))
        if side not in {"left", "right"}:
            side = "left" if (first_side_index + item_index) % 2 == 0 else "right"

        candidate_bands = [
            allowed_bands[(first_band_index + item_index + offset) % len(allowed_bands)]
            for offset in range(len(allowed_bands))
        ]
        band = str(existing.get("band") or "")
        if band not in allowed_bands:
            band = candidate_bands[0]
            for candidate_band in candidate_bands:
                if not any(
                    _items_overlap(item, previous)
                    and str(previous.get("layout", {}).get("side") or "") == side
                    and str(previous.get("layout", {}).get("band") or "") == candidate_band
                    for previous in output
                ):
                    band = candidate_band
                    break

        role = str(item.get("role") or "fact_badge")
        default_width = 0.42 if role in {"checklist", "usage_step"} else 0.40 if role == "product_card" else 0.38
        default_scale = {
            "checklist": 0.94,
            "usage_step": 0.82,
            "fact_badge": 0.92,
            "product_card": 0.96,
            "closing_cta": 1.0,
        }.get(role, 0.94)
        try:
            width_ratio = float(existing.get("max_width_ratio", default_width))
        except (TypeError, ValueError):
            width_ratio = default_width
        try:
            font_scale = float(existing.get("font_scale", default_scale))
        except (TypeError, ValueError):
            font_scale = default_scale
        item["layout"] = {
            "side": side,
            "band": band,
            "max_width_ratio": round(max(0.38, min(0.42, width_ratio)), 3),
            "font_scale": round(max(0.82, min(1.0, font_scale)), 3),
        }
        output.append(item)
    return output


def apply_compliance_to_dynamic_plan(
    plan: dict[str, Any],
    compliance_result: dict[str, Any],
) -> dict[str, Any]:
    output = copy.deepcopy(plan or {})
    items = [item for item in output.get("items", []) or [] if isinstance(item, dict)]
    dropped = list(output.get("dropped", []) or [])
    overlay_violations = [
        item
        for item in (compliance_result or {}).get("violations", []) or []
        if isinstance(item, dict) and str(item.get("source_field") or "") == "overlay"
    ]
    for violation in overlay_violations:
        original = str(violation.get("original_text") or "").strip()
        replacement = str(violation.get("suggested_replacement") or "").strip()
        severity = str(violation.get("severity") or "medium").casefold()
        if not original:
            continue
        retained = []
        for item in items:
            display = _candidate_display_text(item)
            if original.casefold() not in display.casefold():
                retained.append(item)
                continue
            if severity == "low" and replacement:
                retained.append(_replace_in_item(item, original, replacement))
            else:
                dropped.append({
                    "role": item.get("role", ""),
                    "text": display,
                    "reason": f"compliance_{severity}",
                    "violation": original,
                    "start": item.get("start"),
                    "end": item.get("end"),
                })
        items = retained
    output["items"] = items
    output["dropped"] = dropped
    output["source_counts"] = _source_counts(output.get("items", []))
    return output


def remap_dynamic_plan_for_silence(plan: dict[str, Any], silence_plan: dict[str, Any]) -> dict[str, Any]:
    if not silence_plan or not silence_plan.get("trimmed"):
        return copy.deepcopy(plan)
    from silence_trimmer import remap_events_to_compacted_timeline

    output = copy.deepcopy(plan)
    events = []
    speech_events = []
    for item_index, item in enumerate(output.get("items", []) or []):
        if not isinstance(item, dict):
            continue
        events.append({
            **item,
            "_dynamic_item_index": item_index,
            "relative_start": float(item.get("start") or 0.0),
            "relative_end": float(item.get("end") or item.get("start") or 0.0),
        })
        speech = item.get("speech_evidence") if isinstance(item.get("speech_evidence"), dict) else {}
        if speech:
            speech_events.append({
                "_dynamic_item_index": item_index,
                "relative_start": float(speech.get("start") or 0.0),
                "relative_end": float(speech.get("end") or speech.get("start") or 0.0),
            })
    remapped = remap_events_to_compacted_timeline(events, silence_plan)
    remapped_speech = {
        int(item["_dynamic_item_index"]): item
        for item in remap_events_to_compacted_timeline(speech_events, silence_plan)
        if "_dynamic_item_index" in item
    }
    remapped_items = []
    for item in remapped:
        start = float(item.get("relative_start") or 0.0)
        end = float(item.get("relative_end") or 0.0)
        if end <= start + 0.1:
            continue
        item_index = int(item.get("_dynamic_item_index", -1))
        cleaned = {
            key: value
            for key, value in item.items()
            if key not in {
                "_dynamic_item_index",
                "relative_start",
                "relative_end",
                "relative_track",
                "duration",
            }
        }
        cleaned["start"] = round(start, 3)
        cleaned["end"] = round(end, 3)
        speech = cleaned.get("speech_evidence")
        mapped_speech = remapped_speech.get(item_index)
        if isinstance(speech, dict) and mapped_speech:
            speech["start"] = round(float(mapped_speech.get("relative_start") or 0.0), 3)
            speech["end"] = round(float(mapped_speech.get("relative_end") or 0.0), 3)
        remapped_items.append(cleaned)
    output["items"] = remapped_items
    return output


def remap_dynamic_plan_for_speed(plan: dict[str, Any], speed_ramp: float) -> dict[str, Any]:
    if abs(float(speed_ramp or 1.0) - 1.0) <= 0.02:
        return copy.deepcopy(plan)
    output = copy.deepcopy(plan)
    speed = max(0.01, float(speed_ramp))
    for item in output.get("items", []) or []:
        if isinstance(item, dict):
            item["start"] = round(float(item.get("start") or 0.0) / speed, 3)
            item["end"] = round(float(item.get("end") or 0.0) / speed, 3)
            if isinstance(item.get("reveal_offsets"), list):
                item["reveal_offsets"] = [round(float(value) / speed, 3) for value in item["reveal_offsets"]]
            speech = item.get("speech_evidence") if isinstance(item.get("speech_evidence"), dict) else {}
            if speech:
                speech["start"] = round(float(speech.get("start") or 0.0) / speed, 3)
                speech["end"] = round(float(speech.get("end") or 0.0) / speed, 3)
    return output


def compliance_blocking_result(compliance_result: dict[str, Any]) -> dict[str, Any]:
    output = copy.deepcopy(compliance_result or {})
    violations = [
        item
        for item in output.get("violations", []) or []
        if not isinstance(item, dict) or str(item.get("source_field") or "") != "overlay"
    ]
    output["violations"] = violations
    output["violation_count"] = len(violations)
    high_or_medium = any(
        str(item.get("severity") or "").casefold() in {"high", "medium"}
        for item in violations
        if isinstance(item, dict)
    )
    low_unfixed = any(
        str(item.get("severity") or "").casefold() == "low"
        for item in violations
        if isinstance(item, dict)
    ) and not bool(output.get("auto_fixed", False))
    output["passed"] = not high_or_medium and not low_unfixed
    return output


def _document_candidates(
    index: dict[str, Any],
    product_key: str | None,
    enabled_roles: tuple[str, ...],
    moment: dict[str, Any],
    variant,
    *,
    speech_matches: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    facts = facts_for_product(index, product_key) if product_key else []
    grouped: dict[str, list[dict[str, Any]]] = {}
    for fact in facts:
        grouped.setdefault(str(fact.get("role") or ""), []).append(fact)
    output = []
    dropped = []

    for speech_match in speech_matches:
        setting_role = str(speech_match.get("content_role") or "")
        if setting_role not in {"ingredients", "benefits", "usage"}:
            continue
        fact_role = _ROLE_TO_FACT_ROLE[setting_role]
        if setting_role == "ingredients":
            selected = _rotated(
                grouped.get(fact_role, []),
                3,
                moment,
                variant,
                setting_role,
            )
            display_lines = [
                shortened
                for shortened in (_short_exact_text(item.get("text")) for item in selected)
                if shortened
            ]
        else:
            max_words = 6
            concise_options = _concise_fact_options(
                grouped.get(fact_role, []),
                max_words=max_words,
            )
            selected = _rotated(
                concise_options,
                1,
                moment,
                variant,
                setting_role,
            )
            display_lines = [
                str(item.get("_display_text") or "")
                for item in selected
                if str(item.get("_display_text") or "").strip()
            ]
        source = "document"
        evidence = selected
        if not display_lines:
            transcript_text = concise_dynamic_fact_text(
                speech_match.get("excerpt"),
                max_words=6 if setting_role in {"benefits", "usage"} else 18,
            )
            if transcript_text:
                display_lines = [transcript_text]
                source = "transcript"
                evidence = [{"text": transcript_text, "source_excerpt": transcript_text}]
        if not display_lines:
            dropped.append(_dropped_speech_match(speech_match, "no_document_or_transcript_fact"))
            continue

        if setting_role == "ingredients":
            output.append(_candidate(
                role="checklist",
                content_role="ingredients",
                headline=_SECTION_LABELS["ingredients"],
                lines=display_lines,
                source=source,
                evidence=evidence,
                speech_evidence=speech_match,
                priority=100,
            ))
        elif setting_role == "benefits":
            output.append(_candidate(
                role="fact_badge",
                content_role="benefits",
                headline=_SECTION_LABELS["benefits"],
                text=display_lines[0],
                source=source,
                evidence=evidence,
                speech_evidence=speech_match,
                priority=95,
            ))
        else:
            output.append(_candidate(
                role="usage_step",
                content_role="usage",
                headline=_SECTION_LABELS["usage"],
                text=display_lines[0],
                lines=[],
                source=source,
                evidence=evidence,
                speech_evidence=speech_match,
                priority=90,
            ))

    if "cta" in enabled_roles and product_key:
        for item in _rotated(grouped.get("cta", []), 1, moment, variant, "cta"):
            display_text = _short_exact_text(item.get("text"), 72)
            if not display_text:
                continue
            output.append(_candidate(
                role="closing_cta",
                content_role="cta",
                text=display_text,
                source="document",
                evidence=[item],
                priority=78,
            ))
    return output, dropped


def _speech_topic_matches(
    clip_words: list[dict[str, Any]],
    enabled_roles: tuple[str, ...],
    *,
    hook_duration: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    guard = max(0.0, float(hook_duration or 0.0)) + SPEECH_TOPIC_GUARD_SECONDS
    matches = timed_information_topic_windows(clip_words)
    selected = []
    dropped = []
    seen_roles = set()
    for strongest in matches:
        window_matches = [
            {key: value for key, value in strongest.items() if key != "weaker_matches"},
            *(strongest.get("weaker_matches", []) or []),
        ]
        enabled_matches = [
            item
            for item in window_matches
            if str(item.get("content_role") or "") in enabled_roles
        ]
        post_hook = []
        for item in enabled_matches:
            eligible_terms = [
                term
                for term in item.get("term_matches", []) or []
                if float(term.get("start") or 0.0) >= guard
            ]
            if not eligible_terms:
                dropped.append(_dropped_speech_match(item, "before_hook"))
                continue
            adjusted = dict(item)
            adjusted["start"] = min(float(term.get("start") or 0.0) for term in eligible_terms)
            adjusted["matched_terms"] = [str(term.get("term") or "") for term in eligible_terms]
            adjusted["score"] = len(eligible_terms) * 100 + sum(
                10 for term in eligible_terms if " " in str(term.get("term") or "")
            )
            post_hook.append(adjusted)
        if not post_hook:
            continue
        unseen_matches = []
        for item in post_hook:
            if str(item.get("content_role") or "") in seen_roles:
                dropped.append(_dropped_speech_match(item, "duplicate_content_role"))
            else:
                unseen_matches.append(item)
        if not unseen_matches:
            continue
        ordered = sorted(
            unseen_matches,
            key=lambda item: (
                -int(item.get("score") or 0),
                float(item.get("start") or 0.0),
                str(item.get("content_role") or ""),
            ),
        )
        match = ordered[0]
        for weaker in ordered[1:]:
            dropped.append(_dropped_speech_match(weaker, "weaker_overlapping_topic"))
        content_role = str(match.get("content_role") or "")
        seen_roles.add(content_role)
        selected.append({
            "excerpt": _short_exact_text(match.get("excerpt"), 160),
            "start": round(float(match.get("start") or 0.0), 3),
            "end": round(float(match.get("end") or match.get("start") or 0.0), 3),
            "matched_terms": [
                str(term)
                for term in match.get("matched_terms", []) or []
                if str(term).strip()
            ],
            "score": int(match.get("score") or 0),
            "category": str(match.get("category") or ""),
            "content_role": content_role,
        })
    return selected, dropped


def _dropped_speech_match(match: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "role": str(match.get("content_role") or ""),
        "content_role": str(match.get("content_role") or ""),
        "text": _short_exact_text(match.get("excerpt"), 160),
        "reason": reason,
        "start": round(float(match.get("start") or 0.0), 3),
        "end": round(float(match.get("end") or match.get("start") or 0.0), 3),
        "matched_terms": [
            str(term)
            for term in match.get("matched_terms", []) or []
            if str(term).strip()
        ],
        "score": int(match.get("score") or 0),
    }


def _candidate(
    *,
    role: str,
    source: str,
    evidence: list[dict[str, Any]],
    priority: int,
    content_role: str = "",
    headline: str = "",
    text: str = "",
    lines: list[str] | None = None,
    speech_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    output = {
        "role": role,
        "content_role": str(content_role or "").strip(),
        "headline": str(headline or "").strip(),
        "text": str(text or "").strip(),
        "lines": [str(line).strip() for line in lines or [] if str(line).strip()],
        "source": source,
        "evidence": [_public_evidence(item) for item in evidence if isinstance(item, dict)],
        "priority": int(priority),
    }
    if speech_evidence:
        output["speech_evidence"] = {
            key: copy.deepcopy(speech_evidence[key])
            for key in (
                "excerpt",
                "start",
                "end",
                "matched_terms",
                "score",
                "category",
            )
            if key in speech_evidence
        }
    if output["headline"] and role in {"checklist", "usage_step", "fact_badge"}:
        output["headline_role"] = "section_headline"
    return output


def _public_evidence(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item[key]
        for key in ("id", "role", "text", "source_file", "locator", "source_excerpt", "confidence")
        if key in item
    }


def _schedule_items(
    candidates: list[dict[str, Any]],
    duration: float,
    *,
    hook_duration: float,
    settings: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    settings = settings or {}
    guard = min(
        max(0.35, float(hook_duration or 0.0) + SPEECH_TOPIC_GUARD_SECONDS),
        max(0.35, duration - 1.0),
    )
    closing = [item for item in candidates if item.get("role") == "closing_cta"][:1]
    information = sorted(
        [
            item
            for item in candidates
            if str(item.get("content_role") or "") in {"ingredients", "benefits", "usage"}
        ],
        key=lambda item: (
            float((item.get("speech_evidence") or {}).get("start") or 0.0),
            -int((item.get("speech_evidence") or {}).get("score") or 0),
        ),
    )
    dropped = []
    cta_duration = _role_duration(settings, "cta", 1.3)
    cta_start = max(guard, duration - 0.12 - cta_duration) if closing else duration - 0.12
    information_end_limit = max(guard, cta_start - 0.12)
    output: list[dict[str, Any]] = []

    for candidate in information:
        speech = candidate.get("speech_evidence") if isinstance(candidate.get("speech_evidence"), dict) else {}
        item_start = max(guard, float(speech.get("start") or guard))
        content_role = str(candidate.get("content_role") or "")
        display_duration = _role_duration(settings, content_role, 2.6)
        desired_end = min(information_end_limit, item_start + display_duration)
        item = {
            **candidate,
            "start": round(item_start, 3),
            "end": round(desired_end, 3),
        }
        if item.get("role") == "checklist":
            item["reveal_offsets"] = [
                round(index * 0.22, 3)
                for index, _line in enumerate(item.get("lines", []))
            ]

        if desired_end <= item_start + MIN_INFORMATION_DISPLAY_SECONDS - 0.001:
            dropped.append(_dropped_candidate(item, "insufficient_display_time"))
            continue
        if output and item_start < float(output[-1].get("end") or 0.0) + 0.12:
            previous = output[-1]
            shortened_previous_end = item_start - 0.12
            if shortened_previous_end >= float(previous.get("start") or 0.0) + MIN_INFORMATION_DISPLAY_SECONDS:
                previous["end"] = round(shortened_previous_end, 3)
            else:
                previous_score = int((previous.get("speech_evidence") or {}).get("score") or 0)
                current_score = int(speech.get("score") or 0)
                if current_score > previous_score:
                    dropped.append(_dropped_candidate(output.pop(), "weaker_overlapping_topic"))
                else:
                    dropped.append(_dropped_candidate(item, "weaker_overlapping_topic"))
                    continue
        output.append(item)

    if closing:
        item = dict(closing[0])
        item["start"] = round(cta_start, 3)
        item["end"] = round(max(item["start"] + 0.5, duration - 0.12), 3)
        output.append(item)

    return sorted(output, key=lambda item: (float(item.get("start") or 0.0), -int(item.get("priority") or 0))), dropped


def _role_duration(settings: dict[str, dict[str, Any]], role: str, default: float) -> float:
    raw = settings.get(role) if isinstance(settings, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    try:
        return max(1.0, min(6.0, float(raw.get("duration_seconds", default))))
    except (TypeError, ValueError):
        return default


def _adaptive_information_duration(item: dict[str, Any]) -> float:
    line_count = len(item.get("lines", []) or []) + (1 if str(item.get("text") or "").strip() else 0)
    return min(2.8, max(1.8, 1.8 + max(0, line_count - 1) * 0.35))


def _dropped_candidate(item: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "role": str(item.get("role") or ""),
        "content_role": str(item.get("content_role") or ""),
        "text": _candidate_display_text(item),
        "reason": reason,
        "start": round(float(item.get("start") or 0.0), 3),
        "end": round(float(item.get("end") or item.get("start") or 0.0), 3),
    }


def _items_overlap(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_start = float(left.get("start") or 0.0)
    left_end = float(left.get("end") or left_start)
    right_start = float(right.get("start") or 0.0)
    right_end = float(right.get("end") or right_start)
    return min(left_end, right_end) > max(left_start, right_start) + 0.05


def _select_candidates(
    candidates: list[dict[str, Any]],
    target: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ordered = sorted(candidates, key=lambda item: (-int(item.get("priority") or 0), _candidate_display_text(item)))
    selected = ordered[:max(0, target)]
    dropped = [
        _dropped_candidate(item, "mode_limit")
        for item in ordered[max(0, target):]
    ]
    return selected, dropped


def _target_count(duration: float, minimum: int, maximum: int) -> int:
    if duration < 7:
        return minimum
    if duration < 11:
        return min(maximum, minimum + 1)
    if duration < 18:
        return min(maximum, minimum + 2)
    return maximum


def _dynamic_mode(cfg, variant) -> str:
    value = getattr(variant, "dynamic_text_mode", None) if variant is not None else None
    if value is None:
        value = getattr(cfg, "_dynamic_text_mode", getattr(cfg, "DYNAMIC_TEXT_MODE", "balanced"))
    normalized = str(value or "balanced").strip().casefold()
    return normalized if normalized in DYNAMIC_TEXT_MODES else "balanced"


def _dynamic_roles(cfg, variant) -> tuple[str, ...]:
    value = getattr(variant, "dynamic_text_roles", None) if variant is not None else None
    if value is None:
        value = getattr(cfg, "_dynamic_text_roles", DEFAULT_DYNAMIC_TEXT_ROLES)
    if isinstance(value, str):
        value = [part.strip() for part in value.split(",")]
    selected = tuple(role for role in DYNAMIC_TEXT_ROLES if role in set(value or []))
    return selected


def _dynamic_settings(cfg, variant) -> dict[str, dict[str, Any]]:
    value = getattr(variant, "dynamic_text_settings", None) if variant is not None else None
    if not isinstance(value, dict):
        value = getattr(cfg, "_dynamic_text_settings", {})
    defaults = {
        role: {
            "font_size": 50 if role == "cta" else 35,
            "animation": "current",
            "duration_seconds": 1.3 if role == "cta" else 2.6,
        }
        for role in DYNAMIC_TEXT_ROLES
    }
    for role in DYNAMIC_TEXT_ROLES:
        raw = value.get(role) if isinstance(value, dict) else None
        if isinstance(raw, dict):
            defaults[role].update(raw)
    return defaults


def _hook_duration(cfg, variant, duration: float) -> float:
    hook_format = str(getattr(variant, "hook_type", getattr(cfg, "_hook_type", "text")) or "text")
    if hook_format not in {"text", "text_before_after_image", "text_b_roll"}:
        return 0.0
    value = float(getattr(variant, "hook_duration", 0.0) or getattr(cfg, "HOOK_DURATION", 0.0) or 0.0)
    return min(value, duration * 0.4)


def _rotated(
    values: list[dict[str, Any]],
    count: int,
    moment: dict[str, Any],
    variant,
    salt: str,
) -> list[dict[str, Any]]:
    if not values:
        return []
    offset = _seed(moment, variant, salt) % len(values)
    return (values[offset:] + values[:offset])[:count]


def _seed(moment: dict[str, Any], variant, salt: str) -> int:
    raw = "|".join([
        str(moment.get("clip_id") or ""),
        str(moment.get("start") or ""),
        str(getattr(variant, "variant_index", 0) if variant is not None else 0),
        salt,
    ])
    return int(hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12], 16)


def _dedupe_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    output = []
    for item in candidates:
        key = re.sub(r"\W+", " ", _candidate_display_text(item).casefold()).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        output.append(item)
    return output


def _candidate_display_text(item: dict[str, Any]) -> str:
    return " ".join(
        [
            str(item.get("headline") or ""),
            str(item.get("text") or ""),
            *[str(line) for line in item.get("lines", []) or []],
        ]
    ).strip()


def _replace_in_item(item: dict[str, Any], original: str, replacement: str) -> dict[str, Any]:
    output = copy.deepcopy(item)
    pattern = re.compile(re.escape(original), re.IGNORECASE)
    for key in ("headline", "text"):
        output[key] = pattern.sub(replacement, str(output.get(key) or ""))
    output["lines"] = [pattern.sub(replacement, str(line)) for line in output.get("lines", []) or []]
    return output


def _short_exact_text(value: Any, max_chars: int = 92) -> str:
    text = " ".join(str(value or "").split()).strip(" ,.;:-")
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    clauses = re.split(r"(?<=[.;!?])\s+|\s+(?:dan|yang|karena|untuk)\s+", text)
    return clauses[0].strip() if clauses and len(clauses[0]) <= max_chars else ""


def concise_dynamic_fact_text(
    value: Any,
    *,
    max_words: int,
    max_chars: int = 64,
) -> str:
    """Select a short exact clause without rewriting the approved wording."""
    text = " ".join(str(value or "").split()).strip(" ,.;:-")
    if not text:
        return ""
    label_match = re.match(r"^[^:]{1,28}:\s*(.+)$", text)
    if label_match:
        text = label_match.group(1).strip()
    clauses = [
        clause.strip(" ,.;:-\"'")
        for clause in re.split(r"(?<=[.!?])\s+|[,;]\s+|\s+&\s+", text)
        if clause.strip(" ,.;:-\"'")
    ]
    eligible = [
        clause
        for clause in clauses
        if (
            len(clause) <= max_chars
            and len(clause.split()) <= max_words
            and clause.casefold() not in {"setelah pemakaian", "manfaat utama", "hasilnya"}
        )
    ]
    if eligible:
        multiword = [clause for clause in eligible if len(clause.split()) >= 2]
        candidates = multiword or eligible
        return min(candidates, key=lambda clause: (len(clause.split()), len(clause), clause.casefold()))
    if len(text) <= max_chars and len(text.split()) <= max_words:
        return text
    return ""


def _concise_fact_options(
    values: list[dict[str, Any]],
    *,
    max_words: int,
) -> list[dict[str, Any]]:
    options = []
    for fact in values:
        display_text = concise_dynamic_fact_text(
            fact.get("text"),
            max_words=max_words,
        )
        if display_text:
            options.append({**fact, "_display_text": display_text})
    return sorted(
        options,
        key=lambda item: (
            len(str(item.get("_display_text") or "").split()),
            len(str(item.get("_display_text") or "")),
            str(item.get("_display_text") or "").casefold(),
        ),
    )


def _source_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        source = str(item.get("source") or "unknown")
        counts[source] = counts.get(source, 0) + 1
    return counts
