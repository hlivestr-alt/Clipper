#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import logging
import re
from bisect import bisect_left, bisect_right
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from clipper_app.path_safety import UnsafePathError, resolve_within_root
from utils import _parse_json_object, lm_studio_openai_chat_kwargs

log = logging.getLogger("proya.compliance_checker")

COMPLIANCE_SCHEMA_VERSION = 1
COMPLIANCE_OUTPUT_DIR_NAME = "compliance"
SORT_TIER_DIRS = {"export_ready", "review_needed", "rejected"}
ALLOWED_TYPES = {
    "medical_claim",
    "absolute_claim",
    "exaggerated_claim",
    "prohibited_ingredient_claim",
}
ALLOWED_SEVERITIES = {"high", "medium", "low"}
BLOCKING_SEVERITIES = {"high"}

SYSTEM_PROMPT = (
    "Kamu adalah evaluator kepatuhan iklan skincare Indonesia sesuai regulasi BPOM "
    "dan kebijakan iklan TikTok. Nilai klaim pada transkrip livestream skincare "
    "dan teks hook overlay yang akan dirender ke video. "
    "Kembalikan hanya JSON valid tanpa markdown, tanpa komentar, dan tanpa teks tambahan."
)

USER_PROMPT_TEMPLATE = """Produk: {product_name}

Tugas:
1. Cari klaim iklan skincare yang berisiko untuk Indonesia pada transkrip subtitle dan hook overlay.
2. Klasifikasikan tiap pelanggaran menjadi:
   medical_claim, absolute_claim, exaggerated_claim, prohibited_ingredient_claim.
3. Severity hanya high, medium, atau low.
4. High selalu diflag dan tidak boleh auto-publish.
5. Medium diflag dan diberi saran pengganti.
6. Low adalah klaim lunak yang aman diauto-fix.
7. Beri suggested_replacement yang natural untuk subtitle Bahasa Indonesia.
8. Gunakan posisi start/end karakter dari materi gabungan di bawah jika memungkinkan.
9. Jika pelanggaran berasal dari hook overlay, isi "source_field": "hook"; jika dari subtitle, isi "source_field": "transcript".

Contoh replacement:
- "menyembuhkan jerawat" -> "membantu mengurangi tampilan jerawat"
- "menghilangkan flek" -> "membantu memudarkan tampilan flek"
- "pasti cerah" -> "kulit tampak lebih cerah"
- "dijamin halus" -> "kulit terasa lebih halus"
- "100% ampuh" -> "telah digunakan banyak pelanggan"
- "dalam 3 hari sembuh" -> "dalam beberapa hari kulit terasa lebih baik"
- "terbaik di dunia" -> "favorit banyak pelanggan kami"
- "langsung terasa" -> "terasa perbedaannya setelah pemakaian rutin"

Format JSON wajib:
{{
  "passed": false,
  "violations": [
    {{
      "original_text": "teks asli",
      "violation_type": "medical_claim",
      "severity": "high",
      "suggested_replacement": "teks pengganti",
      "source_field": "transcript",
      "position": {{"start": 0, "end": 10}}
    }}
  ],
  "cleaned_transcript": "transkrip dengan low severity yang aman sudah diganti",
  "cleaned_hook_text": "hook dengan low severity yang aman sudah diganti",
  "compliance_summary": "satu kalimat Bahasa Indonesia"
}}

Keyword pre-scan yang memicu pemeriksaan:
{keyword_matches}

Materi gabungan:
{combined_text}
"""


def _rule(
    rule_id: str,
    pattern: str,
    violation_type: str,
    severity: str,
    suggested_replacement: str,
    priority: int,
) -> dict[str, Any]:
    return {
        "id": rule_id,
        "pattern": pattern,
        "violation_type": violation_type,
        "severity": severity,
        "suggested_replacement": suggested_replacement,
        "priority": priority,
    }


RULES: list[dict[str, Any]] = [
    _rule(
        "medical_menyembuhkan_jerawat",
        r"\bmenyembuh\w*\s+jerawat\b",
        "medical_claim",
        "high",
        "membantu mengurangi tampilan jerawat",
        10,
    ),
    _rule(
        "medical_menghilangkan_flek",
        r"\bmenghilang\w*\s+flek\b",
        "medical_claim",
        "high",
        "membantu memudarkan tampilan flek",
        10,
    ),
    _rule(
        "absolute_days_cure",
        r"\bdalam\s+\d+\s+(?:hari|minggu)\s+(?:sembuh|pulih|hilang)\b",
        "medical_claim",
        "high",
        "dalam beberapa hari kulit terasa lebih baik",
        10,
    ),
    _rule(
        "absolute_days_pasti",
        r"\bdalam\s+\d+\s+(?:hari|jam|minggu)\s+pasti\b",
        "absolute_claim",
        "high",
        "hasil dapat berbeda pada tiap orang",
        10,
    ),
    _rule(
        "absolute_100_ampuh",
        r"\b100\s*%\s+ampuh\b",
        "absolute_claim",
        "high",
        "telah digunakan banyak pelanggan",
        10,
    ),
    _rule(
        "exaggerated_best_world",
        r"\bterbaik\s+di\s+dunia\b",
        "exaggerated_claim",
        "medium",
        "favorit banyak pelanggan kami",
        10,
    ),
    _rule(
        "exaggerated_langsung_terasa",
        r"\blangsung\s+terasa\b",
        "exaggerated_claim",
        "medium",
        "terasa perbedaannya setelah pemakaian rutin",
        10,
    ),
    _rule(
        "low_pasti_cerah",
        r"\bpasti\s+cerah\b",
        "absolute_claim",
        "low",
        "kulit tampak lebih cerah",
        10,
    ),
    _rule(
        "low_dijamin_halus",
        r"\bdijamin\s+halus\b",
        "absolute_claim",
        "low",
        "kulit terasa lebih halus",
        10,
    ),
    _rule(
        "prohibited_ingredient",
        r"\b(?:tanpa|bebas|free\s+from)\s+(?:merkuri|mercury|hidrokuinon|hydroquinone|steroid)\b",
        "prohibited_ingredient_claim",
        "high",
        "diformulasikan untuk perawatan kulit harian sesuai aturan kosmetik",
        15,
    ),
    _rule(
        "medical_general",
        r"\b(?:menyembuh\w*|mengobati|memulihkan|terapi|treatment\s+medis|klinisi|dermatologis\s+tested|sembuh)\b",
        "medical_claim",
        "high",
        "membantu merawat tampilan kulit",
        20,
    ),
    _rule(
        "medical_permanent_remove",
        r"\bmenghilang\w*\s+permanen\b",
        "medical_claim",
        "high",
        "membantu menyamarkan tampilan masalah kulit",
        20,
    ),
    _rule(
        "absolute_generic",
        r"\b(?:pasti|dijamin|guaranteed|sudah\s+terbukti|tidak\s+akan|tidak\s+bisa\s+gagal)\b",
        "absolute_claim",
        "high",
        "hasil dapat berbeda pada tiap orang",
        50,
    ),
    _rule(
        "absolute_100_percent",
        r"\b100\s*%\b",
        "absolute_claim",
        "high",
        "telah digunakan banyak pelanggan",
        50,
    ),
    _rule(
        "medium_exaggerated",
        r"\b(?:nomor\s*1|no\.?\s*1|paling\s+ampuh|revolusioner|instan|seketika|within\s+seconds)\b",
        "exaggerated_claim",
        "medium",
        "menjadi pilihan banyak pelanggan",
        30,
    ),
    _rule(
        "medium_before_after_absolute",
        r"\b(?:hilang\s+total|bersih\s+sempurna|putih\s+seketika|cerah\s+dalam\s+\d+\s+jam)\b",
        "exaggerated_claim",
        "medium",
        "kulit tampak lebih terawat dengan pemakaian rutin",
        30,
    ),
    _rule(
        "low_soft_guarantee_benefit",
        r"\b(?:pasti|dijamin)\s+(?:cerah|glowing|halus|mulus|lembap|lembab|putih|bersih|kencang|kenyal)\b",
        "absolute_claim",
        "low",
        "kulit tampak lebih terawat",
        40,
    ),
    _rule(
        "low_unqualified_superlative",
        r"\b(?:tercantik|terbaik|tersempurna|terhalus|terampuh|tercerah)\b",
        "exaggerated_claim",
        "low",
        "favorit banyak pelanggan",
        40,
    ),
]

_COMPILED_RULES = [
    {**rule, "regex": re.compile(rule["pattern"], flags=re.IGNORECASE)}
    for rule in sorted(RULES, key=lambda item: int(item["priority"]))
]
_PRESCAN_REGEX = re.compile(
    "|".join(f"(?:{rule['pattern']})" for rule in RULES),
    flags=re.IGNORECASE,
)

COMPLIANCE_SCORE_FIELDS = (
    "compliance_passed",
    "violation_count",
    "auto_fixed",
    "compliance_blocked",
    "compliance_summary",
    "compliance_file",
)


def check_compliance(
    transcript: str | Path | dict | list | None,
    product_name: str = "general",
    hook_text: str | dict | list | None = None,
    cfg=None,
    lm_client: Any | None = None,
    lm_callable: Callable[[list[dict[str, str]], Any], str] | None = None,
    call_lm: bool = True,
) -> dict[str, Any]:
    """Check one clip transcript and return a JSON-serializable compliance result."""
    if cfg is None:
        try:
            import config as cfg  # type: ignore
        except Exception:
            cfg = None

    transcript_text, word_spans = transcript_to_text_with_spans(transcript)
    transcript_text = " ".join(transcript_text.split())
    hook_display_text = hook_to_text(hook_text)
    combined_text, sections = _combined_text_sections(transcript_text, hook_display_text)
    keyword_matches = pre_scan_keywords(combined_text)
    _annotate_keyword_matches(keyword_matches, sections)
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

    if not keyword_matches:
        return _finalize_result(
            transcript_text=transcript_text,
            hook_text=hook_display_text,
            violations=[],
            cleaned_transcript=transcript_text,
            cleaned_hook_text=hook_display_text,
            auto_fixed=False,
            source="keyword_prescan",
            qwen_called=False,
            keyword_matches=[],
            checked_at=now,
            checked_fields=_checked_fields(transcript_text, hook_display_text),
        )

    keyword_violations = keyword_scan_violations(combined_text)
    qwen_called = False
    qwen_error = ""
    qwen_violations: list[dict[str, Any]] = []
    qwen_cleaned = ""
    qwen_summary = ""

    if call_lm:
        try:
            raw = _call_qwen(
                combined_text,
                product_name,
                keyword_matches,
                cfg,
                lm_client=lm_client,
                lm_callable=lm_callable,
            )
            qwen_called = True
            payload = _parse_json_object(raw)
            qwen_violations = normalize_violations(payload.get("violations", []), combined_text)
            qwen_cleaned = str(payload.get("cleaned_transcript") or "").strip()
            qwen_summary = str(payload.get("compliance_summary") or "").strip()
        except Exception as exc:
            qwen_error = str(exc)
            log.warning("Compliance Qwen check unavailable; using keyword fallback: %s", exc)

    merged_violations = merge_violations(qwen_violations, keyword_violations)
    _annotate_violation_fields(merged_violations, sections)
    cleaned_transcript, transcript_auto_fixed = build_cleaned_field_text(
        transcript_text,
        merged_violations,
        source_field="transcript",
        field_offset=_section_offset(sections, "transcript"),
        auto_fix=bool(getattr(cfg, "COMPLIANCE_AUTO_FIX", True)) if cfg is not None else True,
        qwen_cleaned=qwen_cleaned if not hook_display_text else "",
    )
    cleaned_hook_text, hook_auto_fixed = build_cleaned_field_text(
        hook_display_text,
        merged_violations,
        source_field="hook",
        field_offset=_section_offset(sections, "hook"),
        auto_fix=bool(getattr(cfg, "COMPLIANCE_AUTO_FIX", True)) if cfg is not None else True,
        qwen_cleaned="",
    )
    result = _finalize_result(
        transcript_text=transcript_text,
        hook_text=hook_display_text,
        violations=merged_violations,
        cleaned_transcript=cleaned_transcript,
        cleaned_hook_text=cleaned_hook_text,
        auto_fixed=transcript_auto_fixed or hook_auto_fixed,
        source="qwen_keyword" if qwen_called else "keyword_fallback",
        qwen_called=qwen_called,
        keyword_matches=keyword_matches,
        checked_at=now,
        qwen_error=qwen_error,
        compliance_summary=qwen_summary,
        checked_fields=_checked_fields(transcript_text, hook_display_text),
    )
    if word_spans:
        result["_word_span_count"] = len(word_spans)
    return result


def pre_scan_keywords(transcript_text: str) -> list[dict[str, Any]]:
    matches = []
    for match in _PRESCAN_REGEX.finditer(transcript_text or ""):
        matches.append(
            {
                "text": match.group(0),
                "position": {"start": match.start(), "end": match.end()},
            }
        )
    return matches


def hook_to_text(hook_text: str | dict | list | None) -> str:
    if hook_text is None:
        return ""
    if isinstance(hook_text, dict):
        parts = [
            str(hook_text.get(key) or "").strip()
            for key in ("headline", "subtext", "cta", "hook")
            if str(hook_text.get(key) or "").strip()
        ]
        return " ".join(dict.fromkeys(parts))
    if isinstance(hook_text, list):
        return " ".join(str(item).strip() for item in hook_text if str(item).strip())
    return " ".join(str(hook_text).split())


def _combined_text_sections(transcript_text: str, hook_display_text: str) -> tuple[str, list[dict[str, Any]]]:
    combined = transcript_text or ""
    sections: list[dict[str, Any]] = []
    if transcript_text:
        sections.append({"field": "transcript", "start": 0, "end": len(transcript_text)})
    if hook_display_text:
        prefix = "\n\n[HOOK]\n" if combined else "[HOOK]\n"
        hook_start = len(combined) + len(prefix)
        combined = combined + prefix + hook_display_text
        sections.append({"field": "hook", "start": hook_start, "end": hook_start + len(hook_display_text)})
    return combined, sections


def _checked_fields(transcript_text: str, hook_display_text: str) -> list[str]:
    fields = []
    if transcript_text:
        fields.append("transcript")
    if hook_display_text:
        fields.append("hook")
    return fields


def _section_offset(sections: list[dict[str, Any]], source_field: str) -> int:
    for section in sections:
        if section.get("field") == source_field:
            return int(section.get("start") or 0)
    return 0


def _annotate_keyword_matches(matches: list[dict[str, Any]], sections: list[dict[str, Any]]) -> None:
    for item in matches:
        position = item.get("position") if isinstance(item.get("position"), dict) else {}
        field, local = _field_for_position(
            int(position.get("start") or 0),
            int(position.get("end") or 0),
            sections,
        )
        item["source_field"] = field
        item["field_position"] = {"start": local[0], "end": local[1]}


def _annotate_violation_fields(violations: list[dict[str, Any]], sections: list[dict[str, Any]]) -> None:
    for violation in violations:
        span = _violation_span(violation)
        if span is None:
            continue
        field, local = _field_for_position(span[0], span[1], sections)
        violation["source_field"] = violation.get("source_field") or field
        violation["field_position"] = {"start": local[0], "end": local[1]}


def _field_for_position(
    start: int,
    end: int,
    sections: list[dict[str, Any]],
) -> tuple[str, tuple[int, int]]:
    for section in sections:
        section_start = int(section.get("start") or 0)
        section_end = int(section.get("end") or section_start)
        if start < section_end and end > section_start:
            field = str(section.get("field") or "transcript")
            return field, (max(0, start - section_start), max(0, end - section_start))
    return "transcript", (start, end)


def keyword_scan_violations(transcript_text: str) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    occupied: list[tuple[int, int]] = []
    for rule in _COMPILED_RULES:
        regex = rule["regex"]
        for match in regex.finditer(transcript_text or ""):
            span = (match.start(), match.end())
            if _overlaps_any(span, occupied):
                continue
            occupied.append(span)
            original = match.group(0)
            violations.append(
                {
                    "original_text": original,
                    "violation_type": rule["violation_type"],
                    "severity": rule["severity"],
                    "suggested_replacement": _replacement_for(original, rule),
                    "position": {"start": span[0], "end": span[1]},
                    "source": "keyword",
                    "rule_id": rule["id"],
                }
            )
    violations.sort(key=lambda item: (item["position"]["start"], item["position"]["end"]))
    return violations


def normalize_violations(raw_violations: Any, transcript_text: str) -> list[dict[str, Any]]:
    if not isinstance(raw_violations, list):
        return []
    normalized = []
    for item in raw_violations:
        if not isinstance(item, dict):
            continue
        original = str(item.get("original_text") or "").strip()
        violation_type = _normalize_violation_type(item.get("violation_type"))
        severity = _normalize_severity(item.get("severity"), violation_type)
        position = _normalize_position(item.get("position"), transcript_text, original)
        if not original and position is not None:
            original = transcript_text[position[0] : position[1]].strip()
        if not original:
            continue
        if position is None:
            position = _find_text_position(transcript_text, original)
        if position is None:
            continue
        replacement = str(item.get("suggested_replacement") or "").strip()
        if not replacement:
            replacement = _default_replacement(original, violation_type, severity)
        normalized.append(
            {
                "original_text": original,
                "violation_type": violation_type,
                "severity": severity,
                "suggested_replacement": replacement,
                "position": {"start": position[0], "end": position[1]},
                "source_field": str(item.get("source_field") or "").strip().lower(),
                "source": "qwen",
            }
        )
    normalized.sort(key=lambda item: (item["position"]["start"], item["position"]["end"]))
    return normalized


def merge_violations(
    qwen_violations: list[dict[str, Any]],
    keyword_violations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for violation in list(qwen_violations or []) + list(keyword_violations or []):
        span = _violation_span(violation)
        if span is None:
            continue
        duplicate = False
        for existing in merged:
            existing_span = _violation_span(existing)
            if existing_span is None:
                continue
            if _spans_overlap(span, existing_span):
                same_type = existing.get("violation_type") == violation.get("violation_type")
                same_text = _norm_text(existing.get("original_text")) == _norm_text(violation.get("original_text"))
                if same_type or same_text:
                    duplicate = True
                    break
        if not duplicate:
            merged.append(_public_violation(violation))
    merged.sort(key=lambda item: (item["position"]["start"], item["position"]["end"]))
    return merged


def build_cleaned_transcript(
    transcript_text: str,
    violations: list[dict[str, Any]],
    auto_fix: bool = True,
    qwen_cleaned: str = "",
) -> tuple[str, bool]:
    if not auto_fix:
        return transcript_text, False

    low_violations = [
        violation
        for violation in violations
        if str(violation.get("severity")) == "low"
        and str(violation.get("suggested_replacement") or "").strip()
    ]
    if not low_violations:
        return transcript_text, False

    if qwen_cleaned and _only_low_violations(violations) and qwen_cleaned != transcript_text:
        for violation in low_violations:
            violation["auto_fix_applied"] = True
        return " ".join(qwen_cleaned.split()), True

    cleaned = transcript_text
    applied = False
    occupied: list[tuple[int, int]] = []
    for violation in sorted(low_violations, key=lambda item: _violation_span(item) or (0, 0), reverse=True):
        span = _violation_span(violation)
        if span is None or _overlaps_any(span, occupied):
            continue
        replacement = str(violation.get("suggested_replacement") or "").strip()
        if not replacement:
            continue
        cleaned = cleaned[: span[0]] + replacement + cleaned[span[1] :]
        occupied.append(span)
        violation["auto_fix_applied"] = True
        applied = True
    return " ".join(cleaned.split()), applied


def build_cleaned_field_text(
    field_text: str,
    violations: list[dict[str, Any]],
    source_field: str,
    field_offset: int = 0,
    auto_fix: bool = True,
    qwen_cleaned: str = "",
) -> tuple[str, bool]:
    localized = []
    original_by_span: dict[tuple[int, int], dict[str, Any]] = {}
    for violation in violations:
        if not isinstance(violation, dict):
            continue
        if str(violation.get("source_field") or "transcript") != source_field:
            continue
        span = _violation_span(violation)
        if span is None:
            continue
        local_span = (span[0] - field_offset, span[1] - field_offset)
        if local_span[0] < 0 or local_span[1] > len(field_text) or local_span[1] <= local_span[0]:
            continue
        local = copy.deepcopy(violation)
        local["position"] = {"start": local_span[0], "end": local_span[1]}
        localized.append(local)
        original_by_span[local_span] = violation

    cleaned, applied = build_cleaned_transcript(
        field_text,
        localized,
        auto_fix=auto_fix,
        qwen_cleaned=qwen_cleaned,
    )
    for local in localized:
        if not local.get("auto_fix_applied"):
            continue
        span = _violation_span(local)
        if span is not None and span in original_by_span:
            original_by_span[span]["auto_fix_applied"] = True
    return cleaned, applied


def apply_compliance_to_words(words: list[dict[str, Any]], compliance_result: dict[str, Any]) -> list[dict[str, Any]]:
    """Apply low-severity auto-fixes to word-level subtitles while preserving timing ranges."""
    if not words or not isinstance(compliance_result, dict):
        return words
    if not bool(compliance_result.get("auto_fixed")):
        return words

    transcript_text, spans = transcript_to_text_with_spans(words)
    if not spans:
        return words

    output_words = [copy.deepcopy(word) for word in words]
    replacements = []
    for violation in compliance_result.get("violations", []):
        if not isinstance(violation, dict):
            continue
        if str(violation.get("source_field") or "transcript") != "transcript":
            continue
        if violation.get("severity") != "low" or not violation.get("auto_fix_applied"):
            continue
        span = _violation_span(violation)
        replacement = str(violation.get("suggested_replacement") or "").strip()
        if span is None or not replacement:
            continue
        word_indexes = [
            idx
            for idx, word_span in enumerate(spans)
            if _spans_overlap(span, (word_span["start"], word_span["end"]))
        ]
        if word_indexes:
            replacements.append((word_indexes[0], word_indexes[-1], replacement))

    if not replacements:
        return output_words

    for first_idx, last_idx, replacement in sorted(replacements, key=lambda item: item[0], reverse=True):
        original_slice = output_words[first_idx : last_idx + 1]
        if not original_slice:
            continue
        start_t = _safe_float(original_slice[0].get("start"), 0.0)
        end_t = _safe_float(original_slice[-1].get("end"), start_t)
        tokens = [token for token in replacement.split() if token]
        if not tokens:
            continue
        duration = max(0.001, end_t - start_t)
        step = duration / len(tokens)
        replacement_words = []
        for idx, token in enumerate(tokens):
            replacement_words.append(
                {
                    "word": token,
                    "start": round(start_t + step * idx, 6),
                    "end": round(start_t + step * (idx + 1), 6),
                }
            )
        output_words[first_idx : last_idx + 1] = replacement_words
    return output_words


def apply_compliance_to_hook_payload(hook_payload: dict[str, Any], compliance_result: dict[str, Any]) -> dict[str, str]:
    """Apply low-severity auto-fixes to rendered hook headline/subtext/CTA."""
    payload = {
        "headline": str((hook_payload or {}).get("headline") or "").strip(),
        "subtext": str((hook_payload or {}).get("subtext") or "").strip(),
        "cta": str((hook_payload or {}).get("cta") or "").strip(),
    }
    if not bool((compliance_result or {}).get("auto_fixed")):
        return payload

    for violation in (compliance_result or {}).get("violations", []):
        if not isinstance(violation, dict):
            continue
        if str(violation.get("source_field") or "") != "hook":
            continue
        if violation.get("severity") != "low" or not violation.get("auto_fix_applied"):
            continue
        original = str(violation.get("original_text") or "").strip()
        replacement = str(violation.get("suggested_replacement") or "").strip()
        if not original or not replacement:
            continue
        for key in ("headline", "subtext", "cta"):
            patched, changed = _replace_first_case_insensitive(payload[key], original, replacement)
            payload[key] = patched
            if changed:
                break
    if payload["headline"]:
        payload["headline"] = payload["headline"].upper()
    if payload["cta"]:
        payload["cta"] = payload["cta"].upper()
    return payload


def should_block_result(result: dict[str, Any], cfg=None) -> bool:
    if cfg is not None and not bool(getattr(cfg, "COMPLIANCE_BLOCK_HIGH", True)):
        return False
    for violation in result.get("violations", []):
        if isinstance(violation, dict) and violation.get("severity") in BLOCKING_SEVERITIES:
            return True
    return False


def compliance_path_for_clip(output_path: str | Path, clip_id: str) -> Path:
    output = Path(output_path)
    output_root = compliance_output_root_for_clip(output)
    return _compliance_path_for_run(output_root, clip_id or output.stem)


def compliance_output_root_for_clip(output_path: str | Path) -> Path:
    """Return the canonical run root used for a rendered clip's sidecars."""

    return _sidecar_output_root(Path(output_path)).resolve(strict=False)


def _compliance_path_for_run(output_root: Path, clip_id: str) -> Path:
    safe_id = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(clip_id or "clip")).strip("._") or "clip"
    return resolve_within_root(
        output_root,
        Path(COMPLIANCE_OUTPUT_DIR_NAME) / f"{safe_id}_compliance.json",
    )


def _sidecar_output_root(output_path: Path) -> Path:
    parent = output_path.parent
    if parent.name.casefold().startswith("v") and parent.parent.name.casefold() in SORT_TIER_DIRS:
        return parent.parent.parent
    if parent.name.casefold().startswith("v"):
        return parent.parent
    if parent.name.casefold() in SORT_TIER_DIRS:
        return parent.parent
    return parent


def write_compliance_result(
    path: str | Path,
    result: dict[str, Any],
    *,
    output_root: str | Path,
) -> Path:
    target = Path(path)
    declared_root = Path(output_root).resolve(strict=False)
    target = resolve_within_root(declared_root, target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target = resolve_within_root(declared_root, target)
    _write_json_atomic(target, result)
    return target


def attach_result_to_manifest_row(row: dict[str, Any], result: dict[str, Any], compliance_file: str = "") -> None:
    row["compliance_passed"] = bool(result.get("passed", False))
    row["violation_count"] = int(result.get("violation_count") or 0)
    row["auto_fixed"] = bool(result.get("auto_fixed", False))
    row["compliance_blocked"] = bool(result.get("blocked", False))
    row["compliance_summary"] = str(result.get("compliance_summary") or "")
    if compliance_file:
        row["compliance_file"] = compliance_file


def scan_output_dir(
    output_dir: str | Path,
    working_dir: str | Path | None = None,
    cfg=None,
    force: bool = True,
) -> dict[str, Any]:
    """Re-run compliance on existing manifest rows without rendering clips."""
    if cfg is None:
        import config as cfg  # type: ignore

    output_root = Path(output_dir).resolve(strict=False)
    manifest_path = resolve_within_root(output_root, "manifest.json")
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, list):
        raise ValueError(f"Unsupported manifest format: {manifest_path}")

    resolved_working = Path(working_dir) if working_dir is not None else Path(getattr(cfg, "WORKING_DIR", "working")) / output_root.name
    transcript_path = resolved_working / "transcript.json"
    if not transcript_path.exists() and "__" in output_root.name:
        transcript_path = Path(getattr(cfg, "WORKING_DIR", "working")) / output_root.name.split("__", 1)[0] / "transcript.json"
    if not transcript_path.exists():
        raise FileNotFoundError(f"Transcript not found for compliance re-scan: {transcript_path}")

    transcript_payload = json.loads(transcript_path.read_text(encoding="utf-8"))
    transcript_words = transcript_payload.get("words", []) if isinstance(transcript_payload, dict) else []
    word_index = _build_word_index(transcript_words)

    scanned = 0
    passed = 0
    blocked = 0
    auto_fixed = 0
    violation_total = 0

    scan_rows: list[tuple[dict[str, Any], Path]] = []
    for row in manifest:
        if isinstance(row, dict):
            clip_id = str(row.get("clip_id") or "")
            scan_rows.append((row, _compliance_path_for_run(output_root, clip_id)))

    # Validate the complete write set before the first sidecar is persisted.
    for _row, compliance_path in scan_rows:
        resolve_within_root(output_root, compliance_path)

    for row, compliance_path in scan_rows:
        clip_id = str(row.get("clip_id") or "")
        if compliance_path.exists() and not force:
            result = json.loads(compliance_path.read_text(encoding="utf-8"))
        else:
            clip_words = _get_words_for_clip_indexed(word_index, row.get("start"), row.get("end"))
            hook_source = row.get("hook_overlay") if isinstance(row.get("hook_overlay"), dict) else row.get("hook", "")
            result = check_compliance(
                clip_words,
                str(row.get("product") or "general"),
                hook_text=hook_source,
                cfg=cfg,
            )
            result["blocked"] = should_block_result(result, cfg)
            write_compliance_result(compliance_path, result, output_root=output_root)
        relative_path = _relative_to_output(compliance_path, output_root)
        attach_result_to_manifest_row(row, result, relative_path)
        if result.get("blocked"):
            row["status"] = "compliance_blocked"
        scanned += 1
        passed += 1 if result.get("passed") else 0
        blocked += 1 if result.get("blocked") else 0
        auto_fixed += 1 if result.get("auto_fixed") else 0
        violation_total += int(result.get("violation_count") or 0)

    manifest_path = resolve_within_root(output_root, manifest_path, kind="file")
    _write_json_atomic(manifest_path, manifest)
    update_scores_summary_with_compliance(output_root, manifest)
    return {
        "output_dir": str(output_root.resolve()),
        "manifest_path": str(manifest_path.resolve()),
        "scanned": scanned,
        "passed": passed,
        "blocked": blocked,
        "auto_fixed": auto_fixed,
        "violation_count": violation_total,
    }


def update_scores_summary_with_compliance(output_dir: str | Path, manifest: list[dict[str, Any]]) -> None:
    output_root = Path(output_dir).resolve(strict=False)
    try:
        summary_path = resolve_within_root(output_root, "scores_summary.json")
    except (OSError, UnsafePathError) as exc:
        log.warning("Could not validate scores summary path in %s: %s", output_root, exc)
        return
    if not summary_path.exists():
        return
    try:
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("Could not update compliance fields in %s: %s", summary_path, exc)
        return
    if not isinstance(payload, dict):
        return

    manifest_by_clip = {
        str(row.get("clip_id")): row
        for row in manifest
        if isinstance(row, dict) and row.get("clip_id")
    }

    def attach(target: dict[str, Any], source: dict[str, Any] | None) -> None:
        if not source:
            return
        for key in COMPLIANCE_SCORE_FIELDS:
            if key in source:
                target[key] = source[key]

    for clip in payload.get("clips", []) if isinstance(payload.get("clips"), list) else []:
        if isinstance(clip, dict):
            attach(clip, manifest_by_clip.get(str(clip.get("clip_id"))))

    for group in payload.get("groups", []) if isinstance(payload.get("groups"), list) else []:
        if not isinstance(group, dict):
            continue
        source = (
            manifest_by_clip.get(str(group.get("representative_clip_id")))
            or manifest_by_clip.get(str(group.get("clip_id")))
            or manifest_by_clip.get(str(group.get("base_clip_id")))
        )
        attach(group, source)
        variants = group.get("variants", [])
        if isinstance(variants, list):
            for variant in variants:
                if isinstance(variant, dict):
                    attach(variant, manifest_by_clip.get(str(variant.get("clip_id"))))

    payload["compliance"] = summarize_manifest_compliance(manifest)
    summary_path = resolve_within_root(output_root, summary_path, kind="file")
    _write_json_atomic(summary_path, payload)


def summarize_manifest_compliance(manifest: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [row for row in manifest if isinstance(row, dict) and "compliance_passed" in row]
    return {
        "scanned": len(rows),
        "passed": sum(1 for row in rows if row.get("compliance_passed")),
        "blocked": sum(1 for row in rows if row.get("compliance_blocked")),
        "auto_fixed": sum(1 for row in rows if row.get("auto_fixed")),
        "violation_count": sum(int(row.get("violation_count") or 0) for row in rows),
    }


def transcript_to_text_with_spans(transcript: str | Path | dict | list | None) -> tuple[str, list[dict[str, Any]]]:
    if transcript is None:
        return "", []
    if isinstance(transcript, Path):
        return _read_transcript_path(transcript)
    if isinstance(transcript, str):
        try:
            candidate = Path(transcript)
            if len(transcript) < 260 and candidate.exists():
                return _read_transcript_path(candidate)
        except (OSError, ValueError):
            pass
        return " ".join(transcript.split()), []
    if isinstance(transcript, dict):
        if isinstance(transcript.get("words"), list):
            return _words_to_text_with_spans(transcript["words"])
        if isinstance(transcript.get("text"), str):
            return " ".join(transcript["text"].split()), []
        if isinstance(transcript.get("segments"), list):
            text = " ".join(
                str(segment.get("text", "")).strip()
                for segment in transcript["segments"]
                if isinstance(segment, dict)
            )
            return " ".join(text.split()), []
        return "", []
    if isinstance(transcript, list):
        if all(isinstance(item, str) for item in transcript):
            return " ".join(str(item).strip() for item in transcript if str(item).strip()), []
        return _words_to_text_with_spans(transcript)
    return "", []


def _words_to_text_with_spans(words: list) -> tuple[str, list[dict[str, Any]]]:
    parts = []
    spans = []
    cursor = 0
    for idx, item in enumerate(words or []):
        if isinstance(item, dict):
            token = str(item.get("word", "")).strip()
        else:
            token = str(item).strip()
        if not token:
            continue
        if parts:
            parts.append(" ")
            cursor += 1
        start = cursor
        parts.append(token)
        cursor += len(token)
        spans.append({"index": idx, "start": start, "end": cursor})
    return "".join(parts), spans


def _read_transcript_path(path: Path) -> tuple[str, list[dict[str, Any]]]:
    if path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        return transcript_to_text_with_spans(payload)
    return " ".join(path.read_text(encoding="utf-8").split()), []


def _call_qwen(
    combined_text: str,
    product_name: str,
    keyword_matches: list[dict[str, Any]],
    cfg,
    lm_client: Any | None = None,
    lm_callable: Callable[[list[dict[str, str]], Any], str] | None = None,
) -> str:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": USER_PROMPT_TEMPLATE.format(
                product_name=product_name or "general",
                keyword_matches=json.dumps(keyword_matches[:30], ensure_ascii=False),
                combined_text=combined_text[:8000],
            ),
        },
    ]
    if lm_callable is not None:
        return str(lm_callable(messages, cfg))
    if lm_client is None:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("openai package is not installed") from exc
        lm_client = OpenAI(
            base_url=getattr(cfg, "LM_STUDIO_BASE_URL", "http://localhost:1234/v1"),
            api_key=getattr(cfg, "LM_STUDIO_API_KEY", "lm-studio"),
        )
    model_id = getattr(cfg, "LM_STUDIO_MODEL", "qwen/qwen3.6-27b")
    response = lm_client.chat.completions.create(
        model=model_id,
        messages=messages,
        max_tokens=1024,
        timeout=float(getattr(cfg, "COMPLIANCE_LM_TIMEOUT", 60) or 60),
        **lm_studio_openai_chat_kwargs(cfg, model_id=model_id),
    )
    return (response.choices[0].message.content or "").strip()


def _finalize_result(
    transcript_text: str,
    hook_text: str,
    violations: list[dict[str, Any]],
    cleaned_transcript: str,
    cleaned_hook_text: str,
    auto_fixed: bool,
    source: str,
    qwen_called: bool,
    keyword_matches: list[dict[str, Any]],
    checked_at: str,
    qwen_error: str = "",
    compliance_summary: str = "",
    checked_fields: list[str] | None = None,
) -> dict[str, Any]:
    public_violations = [_public_violation(item) for item in violations]
    high_or_medium = any(item["severity"] in {"high", "medium"} for item in public_violations)
    low_unfixed = any(item["severity"] == "low" for item in public_violations) and not auto_fixed
    passed = not high_or_medium and not low_unfixed
    if not compliance_summary:
        compliance_summary = _summary_sentence(public_violations, passed, auto_fixed)
    return {
        "schema_version": COMPLIANCE_SCHEMA_VERSION,
        "passed": passed,
        "violation_count": len(public_violations),
        "violations": public_violations,
        "checked_fields": checked_fields or ["transcript"],
        "hook_text": hook_text,
        "cleaned_transcript": cleaned_transcript,
        "cleaned_hook_text": cleaned_hook_text,
        "compliance_summary": compliance_summary,
        "auto_fixed": bool(auto_fixed),
        "source": source,
        "qwen_called": bool(qwen_called),
        "qwen_error": qwen_error,
        "keyword_match_count": len(keyword_matches),
        "keyword_matches": keyword_matches[:50],
        "checked_at": checked_at,
    }


def _summary_sentence(violations: list[dict[str, Any]], passed: bool, auto_fixed: bool) -> str:
    if not violations:
        return "Transkrip aman untuk subtitle karena tidak ditemukan klaim iklan berisiko."
    high_count = sum(1 for item in violations if item.get("severity") == "high")
    medium_count = sum(1 for item in violations if item.get("severity") == "medium")
    low_count = sum(1 for item in violations if item.get("severity") == "low")
    if high_count:
        return f"Transkrip diblokir karena ditemukan {high_count} klaim berisiko tinggi."
    if medium_count:
        return f"Transkrip perlu ditinjau karena ditemukan {medium_count} klaim berisiko sedang."
    if low_count and auto_fixed:
        return f"Transkrip lolos setelah {low_count} klaim ringan diperbaiki otomatis."
    if passed:
        return "Transkrip lolos pemeriksaan kepatuhan."
    return "Transkrip perlu ditinjau karena masih ada klaim ringan yang belum diperbaiki."


def _public_violation(violation: dict[str, Any]) -> dict[str, Any]:
    position = violation.get("position") if isinstance(violation.get("position"), dict) else {}
    output = {
        "original_text": str(violation.get("original_text") or ""),
        "violation_type": _normalize_violation_type(violation.get("violation_type")),
        "severity": _normalize_severity(violation.get("severity"), violation.get("violation_type")),
        "suggested_replacement": str(violation.get("suggested_replacement") or ""),
        "position": {
            "start": int(position.get("start") or 0),
            "end": int(position.get("end") or 0),
        },
    }
    for key in ("source", "rule_id", "auto_fix_applied"):
        if key in violation:
            output[key] = violation[key]
    for key in ("source_field", "field_position"):
        if key in violation and violation[key]:
            output[key] = violation[key]
    return output


def _replacement_for(original: str, rule: dict[str, Any]) -> str:
    text = _norm_text(original)
    replacements = {
        "menyembuhkan jerawat": "membantu mengurangi tampilan jerawat",
        "menghilangkan flek": "membantu memudarkan tampilan flek",
        "pasti cerah": "kulit tampak lebih cerah",
        "dijamin halus": "kulit terasa lebih halus",
        "100% ampuh": "telah digunakan banyak pelanggan",
        "terbaik di dunia": "favorit banyak pelanggan kami",
        "langsung terasa": "terasa perbedaannya setelah pemakaian rutin",
    }
    if re.fullmatch(r"dalam\s+\d+\s+(?:hari|minggu)\s+sembuh", text):
        return "dalam beberapa hari kulit terasa lebih baik"
    return replacements.get(text, str(rule.get("suggested_replacement") or "").strip())


def _default_replacement(original: str, violation_type: str, severity: str) -> str:
    text = _norm_text(original)
    for rule in _COMPILED_RULES:
        if rule["regex"].fullmatch(text):
            return _replacement_for(original, rule)
    if violation_type == "medical_claim":
        return "membantu merawat tampilan kulit"
    if violation_type == "absolute_claim":
        return "hasil dapat berbeda pada tiap orang"
    if violation_type == "prohibited_ingredient_claim":
        return "diformulasikan untuk perawatan kulit harian sesuai aturan kosmetik"
    return "menjadi pilihan banyak pelanggan"


def _normalize_violation_type(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in ALLOWED_TYPES:
        return text
    if "medical" in text or "medis" in text:
        return "medical_claim"
    if "absolute" in text or "guarantee" in text:
        return "absolute_claim"
    if "ingredient" in text or "bahan" in text:
        return "prohibited_ingredient_claim"
    return "exaggerated_claim"


def _normalize_severity(value: Any, violation_type: Any) -> str:
    text = str(value or "").strip().lower()
    if text in ALLOWED_SEVERITIES:
        return text
    if _normalize_violation_type(violation_type) in {"medical_claim", "absolute_claim", "prohibited_ingredient_claim"}:
        return "high"
    return "medium"


def _normalize_position(raw_position: Any, transcript_text: str, original: str) -> tuple[int, int] | None:
    if isinstance(raw_position, dict):
        try:
            start = int(raw_position.get("start"))
            end = int(raw_position.get("end"))
            if 0 <= start < end <= len(transcript_text):
                return start, end
        except (TypeError, ValueError):
            pass
    if original:
        return _find_text_position(transcript_text, original)
    return None


def _find_text_position(transcript_text: str, original: str) -> tuple[int, int] | None:
    if not transcript_text or not original:
        return None
    match = re.search(re.escape(original.strip()), transcript_text, flags=re.IGNORECASE)
    if match:
        return match.start(), match.end()
    normalized_original = _norm_text(original)
    normalized_transcript = _norm_text(transcript_text)
    normalized_index = normalized_transcript.find(normalized_original)
    if normalized_index < 0:
        return None
    return normalized_index, normalized_index + len(normalized_original)


def _violation_span(violation: dict[str, Any]) -> tuple[int, int] | None:
    position = violation.get("position")
    if not isinstance(position, dict):
        return None
    try:
        start = int(position.get("start"))
        end = int(position.get("end"))
    except (TypeError, ValueError):
        return None
    if start < 0 or end <= start:
        return None
    return start, end


def _only_low_violations(violations: list[dict[str, Any]]) -> bool:
    return bool(violations) and all(item.get("severity") == "low" for item in violations)


def _overlaps_any(span: tuple[int, int], occupied: list[tuple[int, int]]) -> bool:
    return any(_spans_overlap(span, other) for other in occupied)


def _spans_overlap(first: tuple[int, int], second: tuple[int, int]) -> bool:
    return first[0] < second[1] and second[0] < first[1]


def _norm_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _replace_first_case_insensitive(text: str, original: str, replacement: str) -> tuple[str, bool]:
    match = re.search(re.escape(original), text or "", flags=re.IGNORECASE)
    if not match:
        return text, False
    return text[: match.start()] + replacement + text[match.end() :], True


def _build_word_index(words: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(
        [word for word in words if isinstance(word, dict)],
        key=lambda word: (_safe_float(word.get("start"), 0.0), _safe_float(word.get("end"), 0.0)),
    )
    return {
        "words": ordered,
        "starts": [_safe_float(word.get("start"), 0.0) for word in ordered],
    }


def _get_words_for_clip_indexed(index: dict[str, Any], clip_start: Any, clip_end: Any) -> list[dict[str, Any]]:
    start = _safe_float(clip_start, 0.0)
    end = _safe_float(clip_end, start)
    words = index.get("words", [])
    starts = index.get("starts", [])
    left = bisect_left(starts, start)
    right = bisect_right(starts, end + 0.5)
    clip_words = []
    for word in words[left:right]:
        word_start = _safe_float(word.get("start"), 0.0)
        word_end = _safe_float(word.get("end"), word_start)
        if word_end > end + 0.5:
            continue
        clip_words.append(
            {
                "word": str(word.get("word", "")).strip(),
                "start": round(word_start - start, 6),
                "end": round(word_end - start, 6),
            }
        )
    return clip_words


def _relative_to_output(path: Path, output_root: Path) -> str:
    try:
        return str(path.resolve().relative_to(output_root.resolve())).replace("\\", "/")
    except Exception:
        return str(path)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _write_json_atomic(path: Path, payload: Any) -> None:
    root = path.parent.resolve(strict=False)
    path = resolve_within_root(root, path)
    tmp_path = resolve_within_root(root, path.with_suffix(path.suffix + ".tmp"))
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path = resolve_within_root(root, tmp_path, kind="file")
    path = resolve_within_root(root, path)
    tmp_path.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Check PROYA skincare transcript compliance")
    parser.add_argument("--text", default="", help="Transcript text to scan")
    parser.add_argument("--hook-text", default="", help="Rendered hook text to scan with transcript")
    parser.add_argument("--transcript", default="", help="Transcript file path (.txt or .json)")
    parser.add_argument("--product", default="general", help="Product name")
    parser.add_argument("--output-dir", default="", help="Re-scan an output folder manifest")
    parser.add_argument("--working-dir", default="", help="Working folder with transcript.json")
    parser.add_argument("--no-lm", action="store_true", help="Use keyword fallback only")
    args = parser.parse_args()

    if args.output_dir:
        from clipper_app.bootstrap import build_compliance_service
        from clipper_app.contracts import ComplianceScanCommand

        result = build_compliance_service().scan(ComplianceScanCommand(
            output_dir=args.output_dir,
            working_dir=args.working_dir or None,
            force=True,
        )).model_dump()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    import config as cfg  # type: ignore

    transcript: str | Path
    if args.transcript:
        transcript = Path(args.transcript)
    else:
        transcript = args.text
    result = check_compliance(transcript, args.product, hook_text=args.hook_text, cfg=cfg, call_lm=not args.no_lm)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
