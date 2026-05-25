from __future__ import annotations

import re
from typing import Any


PAIN_PATTERNS: list[tuple[str, str]] = [
    (r"\bmata panda\b|\blingkaran hitam\b", "MATA PANDA"),
    (r"\bflek hitam\b|\bnoda hitam\b|\bflek\b", "FLEK HITAM"),
    (r"\bbekas jerawat\b", "BEKAS JERAWAT"),
    (r"\bjerawat\b", "JERAWAT"),
    (r"\bberuntusan\b", "BERUNTUSAN"),
    (r"\bkemerahan\b|\bmerah\b", "KEMERAHAN"),
    (r"\bkering\b|\bflaky\b", "KULIT KERING"),
    (r"\bkusam\b", "KUSAM"),
    (r"\bgelap\b", "WAJAH GELAP"),
    (r"\bberminyak\b|\bminyakan\b|\boily\b", "MINYAKAN"),
    (r"\bpori\b", "PORI BESAR"),
    (r"\bkerutan\b|\bgaris halus\b", "GARIS HALUS"),
]

BENEFIT_PATTERNS: list[tuple[str, str]] = [
    (r"\bglow(?:ing)?\b", "GLOWING"),
    (r"\bcerah(?:kan)?\b|\bbright(?:ening)?\b", "CERAH"),
    (r"\bputih\b", "CERAH"),
    (r"\blembap\b|\bmelembap(?:kan)?\b|\bhydrat(?:e|ing)\b", "LEMBAP"),
    (r"\bhalus\b|\bsmooth\b", "HALUS"),
    (r"\bbersih(?:kan)?\b|\bclean\b", "BERSIH"),
    (r"\bkalem\b|\btenang\b|\breda\b", "KALEM"),
    (r"\bsegar\b|\bfresh\b", "FRESH"),
    (r"\bpudar\b|\bmemudar(?:kan)?\b|\bsamar\b|\bmenyamar(?:kan)?\b", "LEBIH PUDAR"),
]

PROOF_PATTERNS: list[tuple[str, str]] = [
    (r"\b1x sehari\b", "Dipakai 1x sehari"),
    (r"\b3 hari\b|\b7 hari\b|\b10 hari\b", "Pengalaman pakai beberapa hari"),
    (r"\blangsung\b|\binstan\b", "Teksturnya nyaman dipakai"),
    (r"\btanpa klinik\b|\btanpa treatment\b", "Rutinitas skincare di rumah"),
    (r"\blow budget\b|\bmurah\b|\bhemat\b", "Low budget"),
]

DEFAULT_SUBTEXTS = [
    "Dipakai rutin di rumah",
    "Cocok buat rutinitas harian",
    "Dipakai rutin tiap hari",
    "Bisa masuk step harian",
]

DEFAULT_CTAS = [
    "Cek cara pakainya",
    "Lihat step-nya",
    "Mau lihat produknya?",
    "Cek produknya di sini",
]

TIPS_CTAS = [
    "Pakenya gimana?",
    "Step-nya gimana?",
    "Cek cara pakainya",
]

PRODUCT_CTAS = [
    "Produknya apa?",
    "Cek produknya",
    "Lihat detailnya",
]

RISKY_HOOK_PATTERNS: list[str] = [
    r"\b100\s*%",
    r"\bampuh\b",
    r"\bauto\b",
    r"\bberubah\s+total\b",
    r"\bbersih\s+sempurna\b",
    r"\bcerah\s+dalam\s+\d+\s+(?:jam|hari|minggu)\b",
    r"\bcuma\s+dalam\b",
    r"\bdalam\s+\d+\s+(?:jam|hari|minggu)\b",
    r"\bdijamin\b",
    r"\bguaranteed\b",
    r"\bgila\b",
    r"\bhilang\b",
    r"\bhilang\s+total\b",
    r"\bini\s+bukan\s+filter\b",
    r"\binstan\b",
    r"\blangsung\b",
    r"\bmenghilang\w*\b",
    r"\bmengobati\b",
    r"\bmenyembuh\w*\b",
    r"\bnomor\s*1\b",
    r"\bno\.?\s*1\b",
    r"\bpaling\s+ampuh\b",
    r"\bparah\b",
    r"\bpasti\b",
    r"\bputih\s+seketika\b",
    r"\brevolusioner\b",
    r"\bsecepat\b",
    r"\bseketika\b",
    r"\bstop\b",
    r"\bterbaik\b",
    r"\bterampuh\b",
]

_RISKY_HOOK_REGEX = re.compile(
    "|".join(f"(?:{pattern})" for pattern in RISKY_HOOK_PATTERNS),
    flags=re.IGNORECASE,
)


def _normalize(text: Any) -> str:
    return " ".join(str(text or "").strip().split())


def _seed_from_moment(moment: dict[str, Any]) -> str:
    parts = [
        _normalize(moment.get("clip_id")),
        _normalize(moment.get("product")),
        _normalize(moment.get("hook")),
        _normalize(moment.get("reason")),
        _normalize(moment.get("keyword_category")),
    ]
    return "|".join(parts)


def _stable_pick(options: list[str], seed: str) -> str:
    clean_options = [opt for opt in options if _normalize(opt)]
    if not clean_options:
        return ""
    idx = sum(ord(ch) for ch in seed) % len(clean_options)
    return clean_options[idx]


def _find_label(text: str, patterns: list[tuple[str, str]]) -> str | None:
    for pattern, label in patterns:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return label
    return None


def _dedupe_keep_order(options: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for option in options:
        clean = _normalize(option)
        if not clean:
            continue
        key = clean.upper()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(clean)
    return ordered


def _is_soft_claim_text(text: str) -> bool:
    clean = _normalize(text)
    if not clean:
        return False
    return _RISKY_HOOK_REGEX.search(clean) is None


def _collect_context(moment: dict[str, Any]) -> str:
    chunks = [
        _normalize(moment.get("hook")),
        _normalize(moment.get("reason")),
        _normalize(moment.get("product")),
        _normalize(moment.get("clip_type")),
        _normalize(moment.get("keyword_category")),
    ]
    for keyword in moment.get("keywords_found", []) or []:
        if isinstance(keyword, dict):
            chunks.append(_normalize(keyword.get("word")))
            chunks.append(_normalize(keyword.get("context")))
    return " | ".join(part for part in chunks if part)


def _fallback_problem(benefit: str) -> str:
    mapping = {
        "GLOWING": "KUSAM",
        "CERAH": "KUSAM",
        "LEMBAP": "KULIT KERING",
        "HALUS": "KASAR",
        "BERSIH": "KUSAM",
        "KALEM": "KEMERAHAN",
        "FRESH": "WAJAH CAPEK",
        "LEBIH PUDAR": "FLEK HITAM",
    }
    return mapping.get(benefit, "KUSAM")


def _infer_problem(moment: dict[str, Any], context: str) -> str:
    direct = _find_label(context, PAIN_PATTERNS)
    if direct:
        return direct

    category = _normalize(moment.get("keyword_category")).lower()
    if category == "pain_problem":
        return "KUSAM"
    return ""


def _infer_benefit(moment: dict[str, Any], context: str) -> str:
    direct = _find_label(context, BENEFIT_PATTERNS)
    if direct:
        return direct

    category = _normalize(moment.get("keyword_category")).lower()
    if category in {"attention_benefits", "result_proof"}:
        return "CERAH"
    return "GLOWING"


def _extract_day_claim(context: str) -> int | None:
    match = re.search(r"\b(\d{1,2})\s*hari\b", context, flags=re.IGNORECASE)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _headline_benefit_word(benefit: str) -> str:
    mapping = {
        "LEBIH PUDAR": "SAMAR",
        "FRESH": "SEGAR",
        "KALEM": "TENANG",
    }
    return mapping.get(benefit, benefit)


def _experience_benefit_phrase(benefit: str) -> str:
    mapping = {
        "GLOWING": "TAMPAK GLOWING",
        "CERAH": "TAMPAK LEBIH CERAH",
        "LEMBAP": "TERASA LEBIH LEMBAP",
        "HALUS": "TERASA LEBIH HALUS",
        "BERSIH": "TERASA LEBIH BERSIH",
        "KALEM": "TAMPAK LEBIH TENANG",
        "FRESH": "TERASA LEBIH SEGAR",
        "LEBIH PUDAR": "TAMPAK LEBIH SAMAR",
    }
    return mapping.get(_normalize(benefit).upper(), "TAMPAK LEBIH TERAWAT")


def _experience_problem_phrase(problem: str) -> str:
    clean = _normalize(problem).upper()
    mapping = {
        "FLEK HITAM": "TAMPILAN FLEK",
        "BEKAS JERAWAT": "TAMPILAN BEKAS JERAWAT",
        "JERAWAT": "TAMPILAN JERAWAT",
        "BERUNTUSAN": "TAMPILAN BERUNTUSAN",
        "KEMERAHAN": "TAMPILAN KEMERAHAN",
        "KULIT KERING": "KULIT TERASA KERING",
        "KUSAM": "KULIT KUSAM",
        "WAJAH GELAP": "WAJAH TAMPAK KUSAM",
        "MINYAKAN": "KULIT TERASA BERMINYAK",
        "PORI BESAR": "TAMPILAN PORI",
        "GARIS HALUS": "TAMPILAN GARIS HALUS",
    }
    return mapping.get(clean, clean)


def _infer_subtext(moment: dict[str, Any], context: str, seed: str) -> str:
    direct = _find_label(context, PROOF_PATTERNS)
    if direct:
        return direct

    if re.search(r"\bdipakai rutin\b|\brutin\b|\bsetiap hari\b", context, flags=re.IGNORECASE):
        return "Dipakai rutin tiap hari"

    clip_type = _normalize(moment.get("clip_type")).lower()
    if clip_type == "demo":
        return "Dipakai rutin di rumah"
    if clip_type in {"tips", "qna"}:
        return "Rutinitas skincare di rumah"
    if _normalize(moment.get("product")).lower() not in {"", "general"}:
        return "Cocok buat step harian"
    return _stable_pick(DEFAULT_SUBTEXTS, seed + "|sub")


def _infer_cta(moment: dict[str, Any], seed: str) -> str:
    clip_type = _normalize(moment.get("clip_type")).lower()
    product = _normalize(moment.get("product")).lower()

    if clip_type in {"tips", "qna"}:
        return _stable_pick(TIPS_CTAS, seed + "|cta_tips")
    if product not in {"", "general"}:
        return _stable_pick(PRODUCT_CTAS, seed + "|cta_product")
    return _stable_pick(DEFAULT_CTAS, seed + "|cta_default")


def _build_headline(moment: dict[str, Any], problem: str, benefit: str, seed: str) -> str:
    problem = _normalize(problem)
    benefit = _normalize(benefit)
    base_hook = _normalize(moment.get("hook"))
    context = _collect_context(moment)
    clip_type = _normalize(moment.get("clip_type")).lower()
    product = _normalize(moment.get("product"))
    category = _normalize(moment.get("keyword_category")).lower()
    day_claim = _extract_day_claim(context)
    benefit_word = _headline_benefit_word(benefit)

    if not problem:
        problem = _fallback_problem(benefit)

    problem_phrase = _experience_problem_phrase(problem)
    benefit_phrase = _experience_benefit_phrase(benefit)

    headline_options = [
        f"{problem_phrase}? {benefit_phrase}",
        f"KULIT {benefit_phrase}",
        f"AWALNYA {problem_phrase}",
        f"SEKARANG {benefit_phrase}",
        f"SETELAH RUTIN, {benefit_phrase}",
        f"PENGALAMAN KULIT {benefit_phrase}",
        f"COBA STEP UNTUK {benefit_word}",
    ]

    if category == "result_proof":
        headline_options.extend([
            f"HASIL PEMAKAIAN RUTIN",
            f"KULIT TAMPAK LEBIH TERAWAT",
            f"PENGALAMAN PAKAI {day_claim} HARI" if day_claim else "",
            f"RUTIN PAKAI, {benefit_phrase}",
        ])

    if category == "pain_problem":
        headline_options.extend([
            f"{problem_phrase}? COBA STEP INI",
            f"BANTU RAWAT {problem_phrase}",
        ])

    if clip_type in {"demo", "testimoni"}:
        headline_options.extend([
            f"TESTI PEMAKAIAN RUTIN",
            f"RUTIN PAKAI, {benefit_phrase}",
        ])

    if clip_type in {"tips", "qna"}:
        headline_options.extend([
            f"STEP SKINCARE TANPA RIBET",
            f"CARA PAKAI BIAR NYAMAN",
        ])

    if product and product.lower() not in {"general", ""}:
        headline_options.extend([
            "CEK PRODUK DI LIVE INI",
            f"PAKAI {product}, KULIT TERAWAT",
        ])

    if benefit in {"CERAH", "GLOWING"}:
        headline_options.extend([
            f"{problem_phrase}? CEK STEP INI",
            "BIAR KULIT TAMPAK FRESH",
        ])

    if benefit in {"LEMBAP", "HALUS", "KALEM", "FRESH"}:
        headline_options.extend([
            f"KULIT {benefit_phrase}",
            f"RUTIN PAKAI BIAR {benefit_phrase}",
        ])

    if benefit == "LEBIH PUDAR":
        headline_options.extend([
            f"{problem_phrase} LEBIH SAMAR",
            "NODANYA MAKIN SAMAR",
        ])

    if base_hook:
        cleaned = re.sub(r"[^\w\s?]", " ", base_hook, flags=re.UNICODE)
        cleaned = " ".join(cleaned.upper().split())
        strong_pattern = (
            r"^KULIT\b|^TAMPAK\b|^TERASA\b|^PENGALAMAN\b|^COBA\b|"
            r"^CEK\b|^RUTIN\b|^STEP\b|^CARA\b|^AWALNYA\b|^SEKARANG\b"
        )
        if (
            4 <= len(cleaned) <= 34
            and re.search(strong_pattern, cleaned, flags=re.IGNORECASE)
            and _is_soft_claim_text(cleaned)
        ):
            headline_options.append(cleaned)

    headline_options = _dedupe_keep_order([
        option
        for option in headline_options
        if 4 <= len(_normalize(option)) <= 38 and _is_soft_claim_text(option)
    ])

    if not headline_options:
        headline_options = ["KULIT TAMPAK LEBIH TERAWAT"]

    return _stable_pick(headline_options, seed + "|headline")


def build_hook_payload(moment: dict[str, Any]) -> dict[str, str]:
    seed = _seed_from_moment(moment)
    context = _collect_context(moment)
    benefit = _infer_benefit(moment, context)
    problem = _infer_problem(moment, context)
    headline = _build_headline(moment, problem, benefit, seed)
    subtext = _infer_subtext(moment, context, seed)
    cta = _infer_cta(moment, seed)

    return {
        "headline": _normalize(headline).upper(),
        "subtext": _normalize(subtext),
        "cta": _normalize(cta).upper(),
    }


def ensure_hook_payload(moment: dict[str, Any]) -> dict[str, str]:
    existing = moment.get("hook_overlay")
    if isinstance(existing, dict):
        headline = _normalize(existing.get("headline")).upper()
        subtext = _normalize(existing.get("subtext"))
        cta = _normalize(existing.get("cta")).upper()
        if headline and subtext and cta:
            return {
                "headline": headline,
                "subtext": subtext,
                "cta": cta,
            }

    payload = build_hook_payload(moment)
    moment["hook_overlay"] = payload
    if not _normalize(moment.get("hook")):
        moment["hook"] = payload["headline"]
    return payload
