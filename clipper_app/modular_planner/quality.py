from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass
from functools import lru_cache
from typing import Any, Iterable, Sequence


_SPACE_RE = re.compile(r"\s+")
_TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)


def normalize_transcript(value: str | None) -> str:
    text = unicodedata.normalize("NFKC", value or "").casefold()
    text = text.replace("\u200b", "").replace("\ufeff", "")
    return _SPACE_RE.sub(" ", text).strip()


@dataclass(frozen=True)
class JoinabilityAssessment:
    joinability_score: float
    start_quality: str
    end_quality: str
    reason_codes: tuple[str, ...]
    hard_unusable: bool
    boundary_label: str

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["reason_codes"] = list(self.reason_codes)
        return value


class JoinabilityEvaluator:
    """Conservative, transcript-only boundary quality evaluator."""

    def evaluate(self, transcript: str | None) -> JoinabilityAssessment:
        return self._evaluate_cached(transcript or "")

    @staticmethod
    @lru_cache(maxsize=8192)
    def _evaluate_cached(transcript: str) -> JoinabilityAssessment:
        text = normalize_transcript(transcript)
        words = _TOKEN_RE.findall(text)
        reasons: list[str] = []
        start_penalty = 0.0
        end_penalty = 0.0
        hard_unusable = False

        if not words:
            reasons.append("empty_transcript")
            hard_unusable = True
        elif re.match(r"^[,.;:)\]]", text):
            reasons.append("starts_with_boundary_fragment")
            hard_unusable = True

        if re.match(r"^(?:seperti\s+)?yang\s+tadi\b", text) or re.match(
            r"^(?:seperti|sama)\s+(?:yang|aku|saya)\s+(?:tadi|sebelumnya)\b", text
        ) or re.match(r"^melanjutkan\s+(?:yang|dari)\b", text):
            reasons.append("depends_on_prior_context")
            start_penalty = max(start_penalty, 0.32)
        elif re.match(r"^(?:dan|terus)\s+juga\b", text):
            reasons.append("contextual_leading_connector")
            start_penalty = max(start_penalty, 0.18)
        elif re.match(r"^jadi\s+cuma\s+(?:di|rp|seharga|sekitar|hanya)\b", text):
            reasons.append("contextual_missing_price_setup")
            start_penalty = max(start_penalty, 0.22)
        elif re.match(r"^pemula\s+yang\s+ingin\b", text):
            reasons.append("contextual_audience_fragment")
            start_penalty = max(start_penalty, 0.12)
        elif re.match(r"^(?:dan|atau|sedangkan|sementara|yang|dengan)\b", text):
            reasons.append("contextual_leading_connector")
            start_penalty = max(start_penalty, 0.16)

        # A final connector or an unfilled "etalase nomor" is a clear cut-off.
        # Ordinary openings such as "nah", "ya", "nih", and "jadi" are not
        # evidence of truncation on their own.
        if len(words) >= 3 and re.search(
            r"\b(?:dari|untuk|kalau|dan|atau|dengan|ke|di|yang|karena|supaya|agar|seperti|yaitu|adalah)$",
            text,
        ):
            reasons.append("ends_with_incomplete_connector")
            hard_unusable = True
        elif re.search(r"\b(?:etalase|keranjang|produk)\s+nomor$", text):
            reasons.append("ends_before_required_number")
            hard_unusable = True
        elif transcript.rstrip().endswith((",", ":", "…", "...")):
            reasons.append("soft_trailing_boundary_punctuation")
            end_penalty = max(end_penalty, 0.14)

        start_quality = "unusable" if hard_unusable and not words else (
            "dependent" if start_penalty >= 0.25 else "contextual" if start_penalty else "clean"
        )
        end_quality = "truncated" if hard_unusable and any(
            reason in {"ends_with_incomplete_connector", "ends_before_required_number"}
            for reason in reasons
        ) else "contextual" if end_penalty else "clean"
        score = max(0.0, 1.0 - start_penalty - end_penalty)
        if hard_unusable:
            score = min(score, 0.25)
        label = "Unusable" if hard_unusable else "Clean" if score >= 0.90 else "Contextual"
        return JoinabilityAssessment(
            joinability_score=round(score, 4),
            start_quality=start_quality,
            end_quality=end_quality,
            reason_codes=tuple(reasons),
            hard_unusable=hard_unusable,
            boundary_label=label,
        )


_STOP_WORDS = {
    "ada", "adalah", "aja", "aku", "akan", "atau", "banget", "banyak", "bisa", "buat",
    "cuma", "dan", "dari", "dengan", "di", "dia", "gak", "ini", "itu", "jadi", "juga",
    "kalian", "kalau", "karena", "ke", "kita", "kok", "lebih", "nih", "nya", "oke", "pada",
    "pake", "pakai", "saja", "sama", "sangat", "saya", "seperti", "sih", "sudah", "tapi",
    "terus", "tidak", "tuh", "untuk", "udah", "yang", "ya", "yaitu", "yuk",
    "a", "an", "and", "are", "for", "from", "is", "it", "of", "or", "that", "the", "this",
    "to", "very", "with", "you", "your",
}

_PRODUCT_TERMS = {
    "cleanser", "facial", "wash", "toner", "serum", "cream", "krim", "eye", "mask", "masker",
    "moisturizer", "skincare", "produk", "product", "wajah", "kulit",
}

_CONCEPTS = {
    "dryness": {"dry", "kering", "ketarik", "tertarik"},
    "dullness": {"dull", "kusam"},
    "brightness": {"bright", "brighten", "brightening", "cerah", "mencerahkan"},
    "dark_spots": {"spot", "spots", "flek", "noda", "hitam", "hiperpigmentasi"},
    "oil": {"oil", "oily", "minyak", "berminyak", "sebum"},
    "pores": {"pore", "pores", "pori"},
    "redness": {"redness", "red", "merah", "kemerahan", "iritasi"},
    "texture": {"texture", "tekstur", "kasar", "halus"},
    "moisture": {"moisture", "moist", "hydrate", "hydrating", "lembap", "lembab", "melembapkan"},
    "dirt_cleanse": {
        "dirt", "dust", "pollution", "debu", "polusi", "kotor", "kotoran", "clean", "cleanse",
        "cleansing", "bersih", "membersihkan", "pembersih",
    },
}
_TOKEN_CONCEPT = {token: concept for concept, values in _CONCEPTS.items() for token in values}


def _topical_features(text: str | None) -> tuple[set[str], set[str]]:
    tokens = _TOKEN_RE.findall(normalize_transcript(text))
    concepts = {_TOKEN_CONCEPT[token] for token in tokens if token in _TOKEN_CONCEPT}
    salient = {
        token for token in tokens
        if len(token) >= 4 and token not in _STOP_WORDS and token not in _PRODUCT_TERMS
        and token not in _TOKEN_CONCEPT
    }
    return concepts, salient


def topical_continuity_score(hook_text: str | None, benefits_text: str | None) -> float:
    hook_concepts, hook_tokens = _topical_features(hook_text)
    benefit_concepts, benefit_tokens = _topical_features(benefits_text)
    concept_overlap = len(hook_concepts & benefit_concepts) / max(1, min(len(hook_concepts), len(benefit_concepts)))
    token_overlap = len(hook_tokens & benefit_tokens) / max(1, min(len(hook_tokens), len(benefit_tokens)))
    return round(min(1.0, 0.8 * concept_overlap + 0.2 * token_overlap), 4)


def composition_continuity(items: Sequence[dict[str, Any]]) -> float:
    hooks = [item for item in items if item.get("role") == "hook"]
    benefits = [item for item in items if item.get("role") == "benefits"]
    if not hooks or not benefits:
        return 0.0
    values = [
        topical_continuity_score(hooks[0].get("transcript_text"), item.get("transcript_text"))
        for item in benefits
    ]
    return round(sum(values) / len(values), 4)


def joinability_inventory(rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, int]]:
    evaluator = JoinabilityEvaluator()
    result: dict[str, dict[str, int]] = {}
    for row in rows:
        role = str(row.get("role", "unknown"))
        counts = result.setdefault(role, {"clean": 0, "contextual": 0, "hard_excluded": 0})
        assessment = evaluator.evaluate(row.get("transcript_text"))
        key = "hard_excluded" if assessment.hard_unusable else (
            "clean" if assessment.boundary_label == "Clean" else "contextual"
        )
        counts[key] += 1
    return result
