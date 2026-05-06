# =============================================================================
#  moment_detector.py — LLM-based moment scoring via LM Studio
#  LM Studio exposes an OpenAI-compatible API at localhost:1234/v1
#  Make sure LM Studio is running and a model is loaded before running this.
# =============================================================================

import json
import logging
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from hook_text import build_hook_payload

log = logging.getLogger("proya.moment_detector")

MOMENT_DETECTOR_VERSION = "quality_v2"

# ── System prompt (Bahasa Indonesia + English fallback) ──────────────────────


# Quality-first prompt override. The original prompt above was benefit-heavy,
# but it still allowed weak timestamps and generic moments through. This version
# explicitly targets product-selling clips and rejects dead air/repetition.
SYSTEM_PROMPT = """
Kamu adalah editor TikTok direct-response untuk livestream skincare PROYA 5X Vitamin C.

TUJUAN:
Pilih sedikit clip yang benar-benar layak jual. Lebih baik return [] daripada memilih clip yang random, sepi, atau host hanya mengulang kata.

CLIP BAGUS WAJIB BERISI SALAH SATU FOKUS INI:
1. Produk: host membahas produk tertentu, varian, tekstur, fungsi, cocok untuk siapa.
2. Benefit: hasil/manfaat ke kulit, contoh cerah, glowing, lembap, jerawat/flek/kusam membaik.
3. Ingredients: kandungan seperti Vitamin C, alpha arbutin, tranexamic acid, niacinamide, salicylic acid, hyaluronic acid, collagen, centella, peptide.
4. Cara pakai: step pemakaian, kapan dipakai, berapa kali, cara apply/semprot/oles/bilas.
5. Promo/harga: harga, diskon, voucher, gratis ongkir, etalase, checkout, paket, stok, promo live.

FILTER KERAS:
- Host harus sedang berbicara jelas hampir sepanjang clip.
- Jangan pilih bagian silent/dead air, jeda panjang, baca komentar tanpa konteks, atau host tidak mengatakan apa-apa.
- Jangan pilih clip yang isinya hanya kata berulang seperti "tap tap", "love love", "ya ya ya", atau filler.
- Jangan pilih potongan random yang mulai/berhenti di tengah kalimat tanpa konteks.
- Jangan pilih humor, sapaan, atau interaksi chat kecuali tetap membahas produk/benefit/ingredients/cara pakai/promo.
- Jangan pilih kalimat generik seperti "ini bagus banget" kalau tidak ada detail produk/manfaat/harga/cara pakai.

ATURAN TIMESTAMP:
- Gunakan timestamp dari transkrip yang diberikan. Jangan menebak timestamp.
- Start harus di awal kalimat relevan pertama.
- End harus di akhir kalimat relevan terakhir.
- Pilih rentang natural 25-60 detik. Jangan membuat clip 8-10 detik kecuali sangat kuat.
- Jika momen bagus terlalu pendek, gabungkan dengan kalimat relevan berikutnya, bukan dengan silence.

SKOR:
- 9-10: benefit jelas, ingredients kuat, demo/cara pakai jelas, promo/harga jelas, atau testimoni hasil.
- 7-8: produk dibahas jelas tetapi kurang bukti/hasil.
- Di bawah 7: jangan keluarkan.

KATEGORI KEYWORD:
Tetap gunakan 3 kategori ini untuk kompatibilitas renderer:
1. "attention_benefits": benefit, product focus, ingredients, cara pakai.
2. "result_proof": bukti hasil, before-after, klaim cepat, harga/promo kuat.
3. "pain_problem": masalah kulit yang sedang dibahas sebagai konteks sebelum solusi.

FORMAT OUTPUT:
Kembalikan HANYA JSON array valid. Tidak ada markdown, tidak ada teks lain.
Format setiap objek:
{
  "start": <float, detik dari awal video>,
  "end": <float, detik dari awal video>,
  "segments": [
    {"start": <float>, "end": <float>, "description": "<ringkasan kalimat relevan>"}
  ],
  "score": <float 1-10>,
  "hook": "<headline TikTok max 8 kata, Bahasa Indonesia>",
  "reason": "<alasan singkat kenapa clip ini layak jual>",
  "product": "<nama produk jika disebutkan, atau 'general'>",
  "clip_type": "<demo|testimoni|tips|promo|qna|humor>",
  "keyword_category": "<attention_benefits|result_proof|pain_problem>",
  "keywords_found": [
    {"word": "<kata/frasa>", "category": "<attention_benefits|result_proof|pain_problem>", "context": "<frasa sekitar keyword>"}
  ]
}

Jika ada clip yang MUNGKIN bagus tapi kamu tidak yakin, tetap masukkan dengan score 7.
Hanya return [] jika chunk ini benar-benar tidak ada konten produk/benefit/promo sama sekali.
"""


def _call_lm_studio(client, messages: list, cfg) -> str:
    """Make a single call to LM Studio and return the text response."""
    response = client.chat.completions.create(
        model=cfg.LM_STUDIO_MODEL,
        messages=messages,
        temperature=0.2,       # low temperature = more consistent JSON output
        max_tokens=8192,
        timeout=cfg.LM_STUDIO_TIMEOUT,
    )
    return response.choices[0].message.content.strip()


def _parse_moments_json(raw: str) -> list:
    """Safely parse LLM JSON output. Handles common formatting issues."""
    # Strip markdown code fences if present
    raw = re.sub(r"```(?:json)?", "", raw).strip()

    # Try direct parse first
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict) and "moments" in parsed:
            return parsed["moments"]
    except json.JSONDecodeError:
        pass

    # Try extracting JSON array with regex
    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    log.warning(f"Could not parse LLM output as JSON. Raw output:\n{raw[:300]}...")
    return []


_CONTENT_PATTERNS = {
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
        r"\bminyak\w*\b", r"\boily\b",
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

_FOCUS_TO_RENDERER_CATEGORY = {
    "product": "attention_benefits",
    "benefit": "attention_benefits",
    "ingredient": "attention_benefits",
    "how_to": "attention_benefits",
    "promo_price": "result_proof",
}

_FOCUS_TO_CLIP_TYPE = {
    "product": "tips",
    "benefit": "testimoni",
    "ingredient": "tips",
    "how_to": "demo",
    "promo_price": "promo",
}

_FILLER_TOKENS = {
    "ya", "iya", "nih", "dong", "deh", "kak", "kaka", "kakak", "guys",
    "bestie", "beb", "bebep", "ini", "itu", "yang", "dan", "atau", "di",
    "ke", "dari", "nya", "sih", "kan", "aku", "kamu", "kita", "mereka",
    "untuk", "dengan", "banget", "aja", "cuma", "kalau", "kalo", "jadi",
    "nah", "eh", "hmm", "um", "tap", "love",
}


def _tokenize_text(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", str(text or "").lower())


def _segment_record(seg: dict) -> dict | None:
    try:
        start = float(seg.get("start", 0.0))
        end = float(seg.get("end", 0.0))
    except (TypeError, ValueError):
        return None
    text = " ".join(str(seg.get("text", "")).split())
    if end <= start or not text:
        return None
    return {"start": start, "end": end, "text": text}


def _segments_for_range(chunk: dict, start: float, end: float, cfg) -> tuple[float, float, list[dict], str] | None:
    records = []
    for seg in chunk.get("segments", []) or []:
        record = _segment_record(seg)
        if record:
            records.append(record)

    if not records:
        return None

    selected = []
    for idx, record in enumerate(records):
        overlap = min(record["end"], end) - max(record["start"], start)
        min_overlap = min(0.35, max(0.08, (record["end"] - record["start"]) * 0.2))
        if overlap >= min_overlap:
            selected.append(idx)

    if not selected:
        return None

    first = min(selected)
    last = max(selected)
    min_duration = float(getattr(cfg, "MIN_CLIP_DURATION", 15) or 15)
    max_duration = float(getattr(cfg, "MAX_CLIP_DURATION", 45) or 45)
    max_gap = float(getattr(cfg, "MAX_CLIP_SEGMENT_GAP", 4.0) or 4.0)

    def span_duration() -> float:
        return records[last]["end"] - records[first]["start"]

    while span_duration() < min_duration:
        next_ok = (
            last + 1 < len(records)
            and records[last + 1]["start"] - records[last]["end"] <= max_gap
        )
        prev_ok = (
            first > 0
            and records[first]["start"] - records[first - 1]["end"] <= max_gap
        )

        if next_ok:
            last += 1
        elif prev_ok:
            first -= 1
        else:
            break

    while span_duration() > max_duration and last > first:
        last -= 1

    if span_duration() < min_duration:
        return None

    selected_records = records[first:last + 1]
    text = " ".join(record["text"] for record in selected_records).strip()
    if not text:
        return None

    return records[first]["start"], records[last]["end"], selected_records, text


def _collect_content_hits(text: str) -> dict[str, list[str]]:
    normalized = str(text or "").lower()
    hits = {category: [] for category in _CONTENT_PATTERNS}
    for category, patterns in _CONTENT_PATTERNS.items():
        seen = set()
        for pattern in patterns:
            for match in re.finditer(pattern, normalized, flags=re.IGNORECASE):
                word = " ".join(match.group(0).strip().split())
                if word and word not in seen:
                    seen.add(word)
                    hits[category].append(word)
    return hits


def _dominant_focus(hits: dict[str, list[str]]) -> str:
    priority = {
        "benefit": 50,
        "ingredient": 40,
        "how_to": 30,
        "promo_price": 20,
        "product": 10,
    }
    active = [category for category, words in hits.items() if words]
    if not active:
        return "unknown"
    return max(active, key=lambda category: (len(hits[category]), priority.get(category, 0)))


def _has_product_sales_focus(hits: dict[str, list[str]], word_count: int) -> bool:
    counts = {category: len(set(words)) for category, words in hits.items()}
    total = sum(counts.values())

    if counts["benefit"] >= 1 and (counts["product"] >= 1 or total >= 2):
        return True
    if counts["ingredient"] >= 1 and (counts["product"] >= 1 or counts["benefit"] >= 1 or counts["ingredient"] >= 2):
        return True
    if counts["how_to"] >= 1 and (counts["product"] >= 1 or counts["benefit"] >= 1 or counts["ingredient"] >= 1 or counts["how_to"] >= 2):
        return True
    if counts["promo_price"] >= 2 or (counts["promo_price"] >= 1 and (counts["product"] >= 1 or counts["benefit"] >= 1)):
        return True
    if counts["product"] >= 2 and word_count >= 20:
        return True
    return False


def _repetition_issue(tokens: list[str]) -> str | None:
    if len(tokens) < 8:
        return None

    run_token = None
    run_count = 0
    for token in tokens:
        if token == run_token:
            run_count += 1
        else:
            run_token = token
            run_count = 1
        if run_count >= 3 and token not in {"proya"}:
            return f"repeated token '{token}'"

    significant = [token for token in tokens if token not in _FILLER_TOKENS and len(token) > 1]
    if len(significant) < 12:
        return None

    unique_ratio = len(set(significant)) / len(significant)
    if unique_ratio < 0.35:
        return "low unique-word ratio"

    top_token, top_count = Counter(significant).most_common(1)[0]
    if top_count >= 5 and top_count / len(significant) > 0.32:
        return f"over-repeated word '{top_token}'"

    for phrase_len in (2, 3):
        phrases = [
            tuple(significant[idx:idx + phrase_len])
            for idx in range(0, len(significant) - phrase_len + 1)
        ]
        if not phrases:
            continue
        phrase, count = Counter(phrases).most_common(1)[0]
        if count >= 4:
            return f"repeated phrase '{' '.join(phrase)}'"

    return None


def _context_for_hit(text: str, hit: str) -> str:
    haystack = str(text or "")
    idx = haystack.lower().find(str(hit or "").lower())
    if idx < 0:
        return str(hit or "")[:80]
    snippet = haystack[max(0, idx - 45):idx + len(hit) + 45]
    return " ".join(snippet.split())[:90]


def _keyword_payload_from_hits(text: str, hits: dict[str, list[str]]) -> list[dict]:
    payload = []
    for focus in ("benefit", "ingredient", "how_to", "promo_price", "product"):
        category = _FOCUS_TO_RENDERER_CATEGORY.get(focus, "attention_benefits")
        for word in hits.get(focus, [])[:4]:
            payload.append({
                "word": word,
                "category": category,
                "context": _context_for_hit(text, word),
            })
            if len(payload) >= 12:
                return payload
    return payload


def _merge_keywords(model_keywords: object, transcript_keywords: list[dict]) -> list[dict]:
    merged = []
    seen = set()
    for source in (model_keywords if isinstance(model_keywords, list) else [], transcript_keywords):
        if not isinstance(source, list):
            continue
        for item in source:
            if not isinstance(item, dict):
                continue
            word = str(item.get("word", "")).strip()
            if not word:
                continue
            key = (word.lower(), str(item.get("category", "")).lower())
            if key in seen:
                continue
            seen.add(key)
            merged.append({
                "word": word[:80],
                "category": str(item.get("category", "attention_benefits"))[:40],
                "context": str(item.get("context", ""))[:120],
            })
            if len(merged) >= 16:
                return merged
    return merged


def _evaluate_transcript_quality(text: str, duration: float, cfg) -> dict:
    tokens = _tokenize_text(text)
    word_count = len(tokens)
    min_words = int(getattr(cfg, "MIN_CLIP_WORDS", 18) or 18)
    min_density = float(getattr(cfg, "MIN_SPEECH_WORDS_PER_SECOND", 0.75) or 0.75)

    if word_count < min_words:
        return {"ok": False, "reason": f"too few spoken words ({word_count})"}

    density = word_count / max(duration, 0.1)
    if density < min_density:
        return {"ok": False, "reason": f"low speech density ({density:.2f} wps)"}

    repetition = _repetition_issue(tokens)
    if repetition:
        return {"ok": False, "reason": repetition}

    hits = _collect_content_hits(text)
    if not _has_product_sales_focus(hits, word_count):
        return {"ok": False, "reason": "no strong product/benefit/ingredient/how-to/promo focus"}

    focus = _dominant_focus(hits)
    return {
        "ok": True,
        "reason": "ok",
        "word_count": word_count,
        "speech_density": round(density, 2),
        "content_focus": focus,
        "content_hits": {category: words[:8] for category, words in hits.items() if words},
        "transcript_keywords": _keyword_payload_from_hits(text, hits),
    }


def _validate_moment(m: dict, chunk: dict, cfg) -> dict | None:
    """Validate and clean a single moment dict."""
    try:
        chunk_start = float(chunk.get("chunk_start", 0.0))
        chunk_end = float(chunk.get("chunk_end", chunk_start))
        start = float(m.get("start", 0))
        end = float(m.get("end", 0))
        score = float(m.get("score", 0))

        # Basic sanity checks
        if end <= start:
            return None
        if score < cfg.MIN_SCORE:
            return None

        # Timestamps must be within or near the chunk, then snapped to spoken segments.
        if start < chunk_start - 15 or start > chunk_end + 15:
            return None

        start = max(chunk_start, start)
        end = min(chunk_end, end)
        transcript_window = _segments_for_range(chunk, start, end, cfg)
        if transcript_window is None:
            return None

        start, end, transcript_segments, transcript_text = transcript_window
        duration = end - start
        quality = _evaluate_transcript_quality(transcript_text, duration, cfg)
        if not quality.get("ok"):
            log.debug(
                "Rejecting moment %.1f-%.1f: %s",
                start,
                end,
                quality.get("reason", "quality filter"),
            )
            return None

        focus = quality.get("content_focus", "benefit")
        keyword_category = _FOCUS_TO_RENDERER_CATEGORY.get(focus, "attention_benefits")
        clip_type = str(m.get("clip_type", "")).strip().lower()
        if clip_type not in {"demo", "testimoni", "tips", "promo", "qna", "humor"} or focus == "promo_price":
            clip_type = _FOCUS_TO_CLIP_TYPE.get(focus, "tips")

        keywords_found = _merge_keywords(
            m.get("keywords_found", []),
            quality.get("transcript_keywords", []),
        )

        validated = {
            "start": round(max(0, start - cfg.PAD_START), 2),
            "end": round(end + cfg.PAD_END, 2),
            "score": round(score, 1),
            "hook": str(m.get("hook", "Momen menarik dari livestream PROYA"))[:80],
            "reason": str(m.get("reason", ""))[:150],
            "product": str(m.get("product", "general")),
            "clip_type": clip_type,
            "keyword_category": keyword_category,
            "keywords_found": keywords_found,
            "detector_version": MOMENT_DETECTOR_VERSION,
            "content_focus": focus,
            "selected_text": transcript_text[:500],
            "segments": [
                {
                    "start": round(seg["start"], 2),
                    "end": round(seg["end"], 2),
                    "description": seg["text"][:140],
                }
                for seg in transcript_segments[:12]
            ],
            "quality_checks": {
                "word_count": quality.get("word_count"),
                "speech_density": quality.get("speech_density"),
                "content_hits": quality.get("content_hits", {}),
            },
        }
        hook_overlay = build_hook_payload(validated)
        validated["hook_overlay"] = hook_overlay
        validated["hook"] = hook_overlay["headline"]
        return validated
    except (TypeError, ValueError):
        return None


def _cached_moments_are_current(moments: object) -> bool:
    if not isinstance(moments, list) or not moments:
        return False
    return all(
        isinstance(moment, dict)
        and moment.get("detector_version") == MOMENT_DETECTOR_VERSION
        for moment in moments
    )


def detect_moments(chunks: list, working_dir: str, cfg) -> list:
    """
    Run all transcript chunks through LM Studio to find good clip moments.
    Saves results to JSON cache to avoid re-running on crash.
    """
    moments_path = Path(working_dir) / "moments.json"

    if moments_path.exists():
        try:
            with open(moments_path, "r", encoding="utf-8") as f:
                cached_moments = json.load(f)
        except Exception as exc:
            log.warning(f"Ignoring unreadable moments cache {moments_path}: {exc}")
            cached_moments = None
        if _cached_moments_are_current(cached_moments):
            log.info(f"Loading cached moments from {moments_path}")
            return cached_moments
        log.info(
            "Ignoring stale moments cache so the quality-first detector can re-run: "
            f"{moments_path}"
        )

    try:
        from openai import OpenAI
    except ImportError:
        raise RuntimeError("openai package not installed. Run: pip install openai")

    # ── Connect to LM Studio ─────────────────────────────────────────────────
    client = OpenAI(
        base_url=cfg.LM_STUDIO_BASE_URL,
        api_key=cfg.LM_STUDIO_API_KEY,
    )

    log.info(f"Connected to LM Studio at {cfg.LM_STUDIO_BASE_URL}")
    log.info(f"Model: {cfg.LM_STUDIO_MODEL}")
    max_workers = max(1, int(getattr(cfg, "MOMENT_DETECTOR_WORKERS", 1) or 1))
    max_workers = min(max_workers, max(1, len(chunks)))
    log.info(f"Processing {len(chunks)} transcript chunks with {max_workers} worker(s)...")

    def process_chunk(i: int, chunk: dict) -> tuple[int, list]:
        log.info(f"  Chunk {i+1}/{len(chunks)} | t={chunk['chunk_start']:.0f}s-{chunk['chunk_end']:.0f}s")
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Ini adalah transkrip dari segmen livestream "
                    f"(t={chunk['chunk_start']:.1f}s hingga t={chunk['chunk_end']:.1f}s):\n\n"
                    f"{chunk['text']}\n\n"
                    f"Identifikasi momen bagus dan kembalikan JSON array."
                ),
            },
        ]

        raw = _call_lm_studio(client, messages, cfg)
        raw_moments = _parse_moments_json(raw)
        valid_moments = []
        for m in raw_moments:
            validated = _validate_moment(m, chunk, cfg)
            if validated:
                valid_moments.append(validated)

        log.info(f"    Chunk {i+1}: {len(raw_moments)} detected, {len(valid_moments)} valid (score>={cfg.MIN_SCORE})")
        return i, valid_moments

    failed_chunks = 0
    chunk_results = {}

    if max_workers == 1:
        for i, chunk in enumerate(chunks):
            try:
                result_index, valid_moments = process_chunk(i, chunk)
                chunk_results[result_index] = valid_moments
            except Exception as e:
                log.error(f"    LM Studio error on chunk {i+1}: {e}")
                failed_chunks += 1
                if failed_chunks > 5:
                    log.error("Too many LM Studio failures. Check that LM Studio is running and a model is loaded.")
                    raise
                time.sleep(3)
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(process_chunk, i, chunk): i
                for i, chunk in enumerate(chunks)
            }
            for future in as_completed(future_map):
                i = future_map[future]
                try:
                    result_index, valid_moments = future.result()
                    chunk_results[result_index] = valid_moments
                except Exception as e:
                    log.error(f"    LM Studio error on chunk {i+1}: {e}")
                    failed_chunks += 1
                    if failed_chunks > 5:
                        log.error("Too many LM Studio failures. Check that LM Studio is running and a model is loaded.")
                        raise
                    time.sleep(3)

    all_moments = []
    for i in sorted(chunk_results):
        all_moments.extend(chunk_results[i])

    # ── Deduplicate overlapping moments ──────────────────────────────────────
    all_moments = _deduplicate_moments(all_moments)

    # ── Sort by score descending ──────────────────────────────────────────────
    all_moments.sort(key=lambda m: m["score"], reverse=True)

    # ── Assign clip IDs ───────────────────────────────────────────────────────
    for idx, m in enumerate(all_moments):
        m["clip_id"] = f"clip_{idx+1:04d}"

    log.info(f"Total moments found: {len(all_moments)} (from {len(chunks)} chunks)")

    Path(working_dir).mkdir(parents=True, exist_ok=True)
    with open(moments_path, "w", encoding="utf-8") as f:
        json.dump(all_moments, f, ensure_ascii=False, indent=2)

    log.info(f"Moments saved to {moments_path}")
    return all_moments


def _deduplicate_moments(moments: list, overlap_threshold: float = 0.6) -> list:
    """
    Remove moments that overlap too much with a higher-scored moment.
    Uses Intersection over Union (IoU) on time ranges.
    """
    if not moments:
        return []

    moments_sorted = sorted(moments, key=lambda m: m["score"], reverse=True)
    kept = []

    for candidate in moments_sorted:
        c_start, c_end = candidate["start"], candidate["end"]
        c_dur = c_end - c_start

        is_duplicate = False
        for existing in kept:
            e_start, e_end = existing["start"], existing["end"]

            # Calculate overlap
            overlap_start = max(c_start, e_start)
            overlap_end = min(c_end, e_end)
            overlap = max(0, overlap_end - overlap_start)

            union = max(c_end, e_end) - min(c_start, e_start)
            iou = overlap / union if union > 0 else 0

            if iou > overlap_threshold:
                is_duplicate = True
                break

        if not is_duplicate:
            kept.append(candidate)

    log.info(f"Dedup: {len(moments)} → {len(kept)} moments after removing overlaps")
    return kept
