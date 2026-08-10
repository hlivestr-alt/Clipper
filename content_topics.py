from __future__ import annotations

import re
from typing import Any, Iterable


CONTENT_PATTERNS = {
    "product": [
        r"\bproya\b", r"\b5x\b", r"\bproduk\b", r"\bserum\b", r"\btoner\b",
        r"\bcleanser\b", r"\bmoisturi[sz]er\b", r"\beye\s*cream\b",
        r"\bkrim\b", r"\bcream\b", r"\bsheet\s*mask\b", r"\bmasker\b",
        r"\bskincare\b", r"\bpaket\b", r"\bvarian\b", r"\btekstur\b",
        r"\bkemasan\b",
    ],
    "benefit": [
        r"\bmencerah\w*\b", r"\bcerah\w*\b", r"\bglow\w*\b",
        r"\blemb[ae]p\w*\b", r"\bmoist\w*\b", r"\bjerawat\b", r"\bacne\b",
        r"\bflek\b", r"\bnoda\b", r"\bkusam\b", r"\bpori\w*\b",
        r"\bberuntus\w*\b", r"\bkemerahan\b", r"\bhalus\b", r"\bbersih\w*\b",
        r"\bsegar\b", r"\bfresh\b", r"\bkenyal\b", r"\bkencang\b",
        r"\bantioksidan\b", r"\bhidras\w*\b", r"\bhydrat\w*\b",
        r"\bmemudar\w*\b", r"\bpudar\b", r"\bmenyamarkan\b",
        r"\bmeredakan\b", r"\bmenghilangkan\b", r"\bbekas\b",
        r"\bminyak\w*\b", r"\boily\b", r"\bmanfaat\w*\b", r"\bfungsi\w*\b",
        r"\bkegunaan\w*\b",
    ],
    "ingredient": [
        r"\bvitamin\s*c\b", r"\balpha\s*arbutin\b", r"\barbutin\b",
        r"\btranexamic\b", r"\bniacinamide\b", r"\bsalicylic\b",
        r"\bhyaluronic\b", r"\bcollagen\b", r"\bkolagen\b",
        r"\bcentella\b", r"\bpeptide\b", r"\bretinol\b", r"\bceramide\b",
        r"\btea\s*tree\b", r"\bkandungan\b", r"\bmengandung\b",
        r"\bingredient\w*\b", r"\bbahan\b", r"\bextract\b", r"\bekstrak\b",
        r"\bacid\b", r"\basam\b",
    ],
    "how_to": [
        r"\bpakai\w*\b", r"\bpake\w*\b", r"\bdipakai\b", r"\bpemakaian\b",
        r"\bcara\b", r"\bgunakan\b", r"\bapply\b", r"\baplikasi\w*\b",
        r"\boles\w*\b", r"\bsemprot\w*\b", r"\bspray\b", r"\bbilas\b",
        r"\bcuci\s*muka\b", r"\bstep\b", r"\brutin\b", r"\bpagi\b",
        r"\bmalam\b", r"\bsehari\b", r"\btetes\w*\b", r"\btuang\b",
    ],
    "promo_price": [
        r"\bpromo\b", r"\bdiskon\b", r"\bharga\w*\b", r"\bvoucher\b",
        r"\bgratis\s*ongkir\b", r"\bongkir\b", r"\bcheckout\b",
        r"\bcheck\s*out\b", r"\bco\b", r"\betalase\b", r"\bkeranjang\b",
        r"\bnomor\b", r"\bstok\b", r"\bbeli\b", r"\border\b", r"\bcod\b",
        r"\bbundling\b", r"\bhemat\b", r"\bribu\b", r"\brupiah\b",
        r"\brp\s*\d+", r"\b\d+\s*%", r"\b\d+\s*(?:ribu|rb|k)\b",
    ],
}

INFORMATION_TOPIC_ROLES = {
    "ingredient": "ingredients",
    "benefit": "benefits",
    "how_to": "usage",
}

_FOCUS_PRIORITY = {
    "benefit": 50,
    "ingredient": 40,
    "how_to": 30,
    "promo_price": 20,
    "product": 10,
}

_INFORMATION_CATEGORY_ORDER = {
    "ingredient": 0,
    "benefit": 1,
    "how_to": 2,
}


def collect_content_hits(text: str) -> dict[str, list[str]]:
    normalized = str(text or "").lower()
    hits = {category: [] for category in CONTENT_PATTERNS}
    for category, patterns in CONTENT_PATTERNS.items():
        seen = set()
        for pattern in patterns:
            for match in re.finditer(pattern, normalized, flags=re.IGNORECASE):
                word = " ".join(match.group(0).strip().split())
                if word and word not in seen:
                    seen.add(word)
                    hits[category].append(word)
    return hits


def dominant_focus(hits: dict[str, list[str]]) -> str:
    active = [category for category, words in hits.items() if words]
    if not active:
        return "unknown"
    return max(active, key=lambda category: (len(hits[category]), _FOCUS_PRIORITY.get(category, 0)))


def timed_information_topic_windows(
    words: Iterable[dict[str, Any]],
    *,
    max_gap: float = 0.65,
    max_duration: float = 3.2,
    max_words: int = 14,
) -> list[dict[str, Any]]:
    """Return one strongest information topic per timestamped speech phrase."""
    records = _word_records(words)
    if not records:
        return []

    windows: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for record in records:
        starts_new = bool(
            current
            and (
                record["start"] - current[-1]["end"] >= max_gap
                or record["end"] - current[0]["start"] > max_duration
                or len(current) >= max_words
            )
        )
        if starts_new:
            windows.append(current)
            current = []
        current.append(record)
    if current:
        windows.append(current)

    output = []
    for window_index, window in enumerate(windows):
        phrase, spans = _phrase_with_spans(window)
        matches = []
        for category in INFORMATION_TOPIC_ROLES:
            details = _category_match_details(phrase, spans, window, category)
            if not details:
                continue
            matches.append({
                "category": category,
                "content_role": INFORMATION_TOPIC_ROLES[category],
                "excerpt": phrase,
                "start": details["start"],
                "end": window[-1]["end"],
                "matched_terms": details["matched_terms"],
                "term_matches": details["term_matches"],
                "distinct_hits": details["distinct_hits"],
                "specific_hits": details["specific_hits"],
                "score": details["score"],
                "first_match_offset": details["first_match_offset"],
                "window_index": window_index,
            })
        if not matches:
            continue

        ordered = sorted(
            matches,
            key=lambda item: (
                -int(item["distinct_hits"]),
                -int(item["specific_hits"]),
                float(item["start"]),
                _INFORMATION_CATEGORY_ORDER.get(str(item["category"]), 99),
            ),
        )
        winner = dict(ordered[0])
        winner["weaker_matches"] = [
            {
                "category": item["category"],
                "content_role": item["content_role"],
                "matched_terms": item["matched_terms"],
                "term_matches": item["term_matches"],
                "score": item["score"],
                "start": item["start"],
                "end": item["end"],
                "excerpt": item["excerpt"],
            }
            for item in ordered[1:]
        ]
        output.append(winner)
    return output


def _word_records(words: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    records = []
    for item in words or []:
        if not isinstance(item, dict):
            continue
        text = " ".join(str(item.get("word") or "").split())
        if not text:
            continue
        try:
            start = max(0.0, float(item.get("start") or 0.0))
            end = max(start, float(item.get("end") or start))
        except (TypeError, ValueError):
            continue
        records.append({"word": text, "start": start, "end": end})
    return sorted(records, key=lambda item: (item["start"], item["end"], item["word"]))


def _phrase_with_spans(
    window: list[dict[str, Any]],
) -> tuple[str, list[tuple[int, int]]]:
    parts = []
    spans = []
    cursor = 0
    for record in window:
        if parts:
            cursor += 1
        text = record["word"]
        spans.append((cursor, cursor + len(text)))
        parts.append(text)
        cursor += len(text)
    return " ".join(parts), spans


def _category_match_details(
    phrase: str,
    spans: list[tuple[int, int]],
    window: list[dict[str, Any]],
    category: str,
) -> dict[str, Any] | None:
    found = []
    normalized = phrase.lower()
    for pattern in CONTENT_PATTERNS.get(category, []):
        for match in re.finditer(pattern, normalized, flags=re.IGNORECASE):
            term = " ".join(match.group(0).strip().split())
            if term:
                found.append((term, match.start(), match.end()))
    if not found:
        return None

    deduped = []
    seen = set()
    for term, start, end in sorted(found, key=lambda item: (item[1], item[2], item[0])):
        key = term.casefold()
        if key in seen:
            continue
        seen.add(key)
        deduped.append((term, start, end))

    term_matches = []
    for term, term_start, term_end in deduped:
        first_word_index = _word_index_for_offset(term_start, spans)
        last_word_index = _word_index_for_offset(max(term_start, term_end - 1), spans)
        term_matches.append({
            "term": term,
            "start": window[first_word_index]["start"],
            "end": window[last_word_index]["end"],
        })
    first_offset = min(item[1] for item in deduped)
    first_word_index = _word_index_for_offset(first_offset, spans)
    distinct_hits = len(deduped)
    specific_hits = sum(1 for term, _start, _end in deduped if " " in term)
    score = distinct_hits * 100 + specific_hits * 10
    return {
        "start": window[first_word_index]["start"],
        "matched_terms": [item[0] for item in deduped],
        "term_matches": term_matches,
        "distinct_hits": distinct_hits,
        "specific_hits": specific_hits,
        "score": score,
        "first_match_offset": first_offset,
    }


def _word_index_for_offset(offset: int, spans: list[tuple[int, int]]) -> int:
    for index, (span_start, span_end) in enumerate(spans):
        if offset < span_end or span_start >= offset:
            return index
    return max(0, len(spans) - 1)
