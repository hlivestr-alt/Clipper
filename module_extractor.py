from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import subprocess
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from utils import _path_identity, lm_studio_openai_chat_kwargs

log = logging.getLogger("proya.module_extractor")

MODULE_SCHEMA_VERSION = 2
SUPPORTED_MODULE_SCHEMA_VERSIONS = {1, 2}
MODULE_EXTRACTOR_VERSION = "module_extractor_v1"

QUALITY_APPROVED = "approved"
QUALITY_NEEDS_REVIEW = "needs_review"
QUALITY_BLOCKED = "blocked"
QUALITY_NO_VISUAL_EVENTS = "no_visual_events"
REVIEW_PENDING = "pending"

PRODUCT_FOLDERS = ("cleanser", "toner", "serum", "eye_cream", "mask", "skin_cream")
ROLE_FOLDERS = ("hook", "main", "cta")
POST_CUT_FAILURE_REASONS = {
    "ffmpeg_cut_failed",
    "ffprobe_failed",
    "validation_failed",
    "filename_collision",
}
INTERNAL_CANDIDATE_KEYS = {
    "_cached_extraction_policy_hash",
    "_previous_extraction_status",
    "_previous_rejection_detail",
    "_previous_rejection_reason",
}

PRODUCT_ALIASES = {
    "cleanser": (
        "cleanser",
        "face wash",
        "facial wash",
        "sabun muka",
        "sabun wajah",
        "pembersih wajah",
        "cleansernya",
    ),
    "toner": ("toner", "tonernya"),
    "serum": ("serum", "vitamin c serum", "serumnya"),
    "eye_cream": ("eye cream", "eyecream", "krim mata", "cream mata", "mata panda"),
    "mask": ("mask", "masker", "sheet mask", "sheetmask"),
    "skin_cream": (
        "skin cream",
        "cream",
        "krim",
        "moisturizer",
        "moisturiser",
        "pelembap",
        "night cream",
        "day cream",
    ),
}

ROLE_ALIASES = {
    "hook": ("hook", "opening", "pembuka"),
    "main": ("main", "content", "isi", "utama"),
    "cta": ("cta", "closing", "penutup", "promo", "konversi"),
}

SENTENCE_END_RE = re.compile(r"[.!?]+[\"')\]]*$")
VOD_SOURCE_FILENAME_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})-\d{2}-\d{2}-\d{2}\.mp4$",
    re.IGNORECASE,
)

SYSTEM_PROMPT = """
Kamu adalah editor livestream skincare PROYA untuk membuat perpustakaan modul video mentah.

Tugasmu:
- Cari segmen pendek yang bisa dipakai ulang sebagai modul iklan.
- Klasifikasikan hanya menjadi role: hook, main, atau cta.
- Pilih produk hanya dari: cleanser, toner, serum, eye_cream, mask, skin_cream.
- Jangan pilih produk general atau tidak jelas.
- Jangan pilih bagian hening, filler, sapaan kosong, atau obrolan chat tanpa nilai jual.

Definisi role:
- hook: pembuka 4-8 detik yang kuat, seperti masalah kulit, pertanyaan penasaran, klaim mengejutkan, atau benefit kuat.
- main: isi utama 15-45 detik, seperti benefit produk, kandungan, cara pakai, problem-solution, demo, atau before-after.
- cta: penutup 4-12 detik, seperti harga, diskon, voucher, stok terbatas, hari ini saja, checkout, order sekarang.

Aturan penting:
- Timestamp harus memakai detik dari transkrip. Jangan mengarang timestamp.
- Start harus dekat awal kalimat.
- End harus dekat akhir kalimat.
- Semua alasan dan teks harus Bahasa Indonesia.
- Lebih baik return sedikit kandidat yang kuat daripada banyak kandidat lemah.

Format output:
Return HANYA JSON array valid. Tidak ada markdown.
Setiap item:
{
  "product": "serum",
  "role": "hook",
  "start_hint": 123.4,
  "end_hint": 129.1,
  "target_duration": 6.0,
  "confidence": 0.85,
  "transcript_text": "teks kandidat",
  "classification_reason": "alasan singkat Bahasa Indonesia",
  "suggested_hook": "headline pendek Bahasa Indonesia"
}
""".strip()


def extract_modules(
    video_path: str,
    transcript: dict,
    moments: list[dict] | None,
    working_dir: str,
    cfg,
    force: bool = False,
) -> dict[str, Any]:
    """Classify, cut, validate, and index reusable raw modules."""
    _require_portalocker()

    source = Path(video_path)
    working = Path(working_dir)
    library = Path(getattr(cfg, "MODULE_LIBRARY_DIR", r"D:\proya_modules"))
    working.mkdir(parents=True, exist_ok=True)
    _ensure_library_layout(library)

    timings: dict[str, float] = {}
    stage_start = time.perf_counter()
    candidates = _load_or_classify_candidates(source, transcript, working, cfg)
    timings["load_candidates"] = time.perf_counter() - stage_start
    stage_start = time.perf_counter()
    _reset_candidate_annotations(candidates)
    transcript_words = _normalized_transcript_words(transcript)
    sentences = build_sentence_boundaries(transcript, cfg, words=transcript_words)
    timings["boundaries"] = time.perf_counter() - stage_start
    if not sentences:
        log.warning("Module extraction skipped: transcript has no usable sentence boundaries")
        for candidate in candidates:
            _annotate_candidate(candidate, "rejected", "no_sentence_boundaries")
        _write_candidate_cache(source, working, cfg, candidates)
        stats = _empty_result(len(candidates), reason="no_sentence_boundaries")
        _log_rejection_breakdown(stats)
        return stats

    stats = _new_stats(len(candidates))

    stage_start = time.perf_counter()
    with library_index_lock(library, cfg):
        index = _read_index_snapshot_or_rebuild(library, cfg)
    timings["initial_index"] = time.perf_counter() - stage_start
    known_records = [record for record in index.get("modules", []) if _record_media_exists(record)]
    new_records: list[dict[str, Any]] = []
    role_cut_counts: dict[tuple[str, str], int] = {}
    role_candidate_cap = max(0, int(getattr(cfg, "MODULE_MAX_CANDIDATES_PER_ROLE", 0) or 0))

    stage_start = time.perf_counter()
    for candidate in candidates:
        previous_failure = _previous_post_cut_failure(candidate, cfg, force=force)
        if previous_failure:
            stats["skipped_previous_failure"] += 1
            _count_reject(stats, previous_failure, phase="post_cut")
            _annotate_candidate(candidate, "failed", previous_failure)
            continue

        snapped, reject_reason = snap_to_sentence_boundaries(
            candidate,
            transcript,
            cfg,
            sentences=sentences,
            words_all=transcript_words,
        )
        if snapped is None:
            _count_reject(stats, reject_reason or "invalid_candidate", phase="pre_cut")
            _annotate_candidate(candidate, "rejected", reject_reason or "invalid_candidate")
            continue

        candidate["boundary_mode"] = snapped.get("boundary_mode")
        snapped["source_video"] = str(source.resolve())
        snapped["source_video_identity"] = _path_identity(source)
        snapped["evidence_context_text"] = transcript_context_text(
            transcript,
            float(snapped.get("start", 0.0)),
            float(snapped.get("end", 0.0)),
            cfg,
            words=transcript_words,
        )
        _copy_snapped_annotation(candidate, snapped)

        if not product_has_transcript_evidence(snapped["product"], snapped, cfg):
            _count_reject(stats, "product_evidence_missing", phase="pre_cut")
            _annotate_candidate(candidate, "rejected", "product_evidence_missing")
            continue

        output_path = module_output_path(library, snapped, source, cfg)
        duplicate = _find_duplicate_module(
            snapped,
            known_records + new_records,
            output_path=output_path,
            force=force,
            cfg=cfg,
        )
        if duplicate is not None:
            stats["skipped_duplicate"] += 1
            _annotate_candidate(candidate, "skipped_duplicate", "duplicate")
            continue

        role_cap_key = (str(snapped.get("product") or ""), str(snapped.get("role") or ""))
        if role_candidate_cap > 0 and role_cut_counts.get(role_cap_key, 0) >= role_candidate_cap:
            stats["skipped_candidate_cap"] = stats.get("skipped_candidate_cap", 0) + 1
            _count_reject(stats, "candidate_cap_reached", phase="pre_cut")
            _annotate_candidate(candidate, "skipped_candidate_cap", "candidate_cap_reached")
            continue

        try:
            record = cut_and_register_module(
                snapped,
                video_path=str(source),
                output_path=output_path,
                cfg=cfg,
                force=force,
            )
        except ModuleExtractionError as exc:
            stats["failed"] += 1
            _count_reject(stats, exc.reason, phase="post_cut")
            _annotate_candidate(candidate, "failed", exc.reason)
            log.warning("Module rejected at %.3fs: %s", snapped.get("start", 0.0), exc)
            continue

        if record.get("status") == "skipped_existing":
            stats["skipped_existing"] += 1
        else:
            stats["accepted"] += 1
            if role_candidate_cap > 0:
                role_cut_counts[role_cap_key] = role_cut_counts.get(role_cap_key, 0) + 1
        if record.get("boundary_mode") == "word_boundary_fallback":
            stats["word_boundary_fallback"] += 1
            _annotate_candidate(candidate, "word_boundary_fallback", None, record=record)
        else:
            status = "skipped_existing" if record.get("status") == "skipped_existing" else "accepted"
            _annotate_candidate(candidate, status, None, record=record)
        stats["modules"].append(record)
        new_records.append(record)
    timings["candidate_loop"] = time.perf_counter() - stage_start

    stage_start = time.perf_counter()
    if stats["accepted"] > 0 or stats["skipped_existing"] > 0:
        with library_index_lock(library, cfg):
            final_index = rebuild_library_index(library, cfg, write=True)
    else:
        final_index = index
    timings["final_index"] = time.perf_counter() - stage_start
    stats["index_path"] = str((library / "index.json").resolve())
    stats["library_module_count"] = len(final_index.get("modules", []))

    stage_start = time.perf_counter()
    _write_candidate_cache(source, working, cfg, candidates)
    timings["cache_write"] = time.perf_counter() - stage_start
    stats["timings"] = {key: round(value, 3) for key, value in timings.items()}

    log.info(
        "Module extraction complete: candidates=%s accepted=%s existing=%s duplicate=%s rejected_total=%s rejected_pre_cut=%s failed_post_cut=%s failed=%s skipped_previous_failure=%s word_boundary_fallback=%s",
        stats["candidates"],
        stats["accepted"],
        stats["skipped_existing"],
        stats["skipped_duplicate"],
        stats["rejected_total"],
        stats["rejected_pre_cut"],
        stats["failed_post_cut"],
        stats["failed"],
        stats["skipped_previous_failure"],
        stats["word_boundary_fallback"],
    )
    log.info(
        "Module extraction timings: %s",
        " ".join(f"{key}={value:.1f}s" for key, value in timings.items()),
    )
    _log_rejection_breakdown(stats)
    return stats


def classify_module_candidates(chunks: list[dict], cfg) -> list[dict[str, Any]]:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("openai package not installed. Run: pip install openai") from exc

    client = OpenAI(
        base_url=getattr(cfg, "LM_STUDIO_BASE_URL", "http://localhost:1234/v1"),
        api_key=getattr(cfg, "LM_STUDIO_API_KEY", "lm-studio"),
    )
    max_workers = max(1, int(getattr(cfg, "MODULE_CLASSIFIER_WORKERS", 1) or 1))
    max_workers = min(max_workers, max(1, len(chunks)))

    if max_workers == 1:
        results = []
        for index, chunk in enumerate(chunks):
            results.extend(_classify_chunk(client, index, chunk, cfg))
        return results

    from concurrent.futures import ThreadPoolExecutor, as_completed

    results_by_index: dict[int, list[dict[str, Any]]] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(_classify_chunk, client, index, chunk, cfg): index
            for index, chunk in enumerate(chunks)
        }
        for future in as_completed(future_map):
            index = future_map[future]
            results_by_index[index] = future.result()

    results = []
    for index in sorted(results_by_index):
        results.extend(results_by_index[index])
    return results


def snap_to_sentence_boundaries(
    candidate: dict[str, Any],
    transcript: dict,
    cfg,
    sentences: list[dict[str, Any]] | None = None,
    words_all: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    product = canonical_product(candidate.get("product") or candidate.get("product_name"))
    if not product:
        return None, "unknown_product"

    role = canonical_role(candidate.get("role"))
    if not role:
        return None, "unknown_role"

    confidence = _safe_float(candidate.get("confidence"), 0.0)
    min_confidence = float(getattr(cfg, "MODULE_CLASSIFICATION_MIN_CONFIDENCE", 0.6) or 0.6)
    if confidence < min_confidence:
        return None, "low_confidence"

    start_hint = _safe_float(candidate.get("start_hint", candidate.get("start")), None)
    if start_hint is None:
        return None, "missing_start"

    limits = role_duration_limits(role, cfg)
    target = _target_duration(candidate, role, limits)
    words_all = words_all if words_all is not None else _normalized_transcript_words(transcript)
    sentences = sentences if sentences is not None else build_sentence_boundaries(transcript, cfg, words=words_all)
    if not sentences:
        return _snap_to_word_boundary_fallback(
            candidate,
            transcript,
            product,
            role,
            confidence,
            start_hint,
            limits,
            target,
            words_all=words_all,
            reason="no_sentence_boundaries",
        )

    start_sentence = _sentence_for_start_hint(sentences, start_hint)
    if start_sentence is None:
        return _snap_to_word_boundary_fallback(
            candidate,
            transcript,
            product,
            role,
            confidence,
            start_hint,
            limits,
            target,
            words_all=words_all,
            reason="start_outside_transcript",
        )

    start = float(start_sentence["start"])
    min_end = start + limits["min"]
    max_end = start + limits["max"]
    target_end = start + target
    tolerance = float(getattr(cfg, "MODULE_SENTENCE_BOUNDARY_TOLERANCE", 2.0) or 2.0)

    eligible = [s for s in sentences if float(s["end"]) >= min_end - 1e-6 and float(s["end"]) <= max_end + 1e-6]
    if not eligible:
        return _snap_to_word_boundary_fallback(
            candidate,
            transcript,
            product,
            role,
            confidence,
            start_hint,
            limits,
            target,
            words_all=words_all,
            start=start,
            reason=f"{role}_duration_outside_bounds",
        )

    within_tolerance = [s for s in eligible if abs(float(s["end"]) - target_end) <= tolerance]
    if within_tolerance:
        end_sentence = min(within_tolerance, key=lambda s: abs(float(s["end"]) - target_end))
    else:
        after_target = [s for s in eligible if float(s["end"]) >= target_end]
        if not after_target:
            return _snap_to_word_boundary_fallback(
                candidate,
                transcript,
                product,
                role,
                confidence,
                start_hint,
                limits,
                target,
                words_all=words_all,
                start=start,
                reason=f"{role}_no_boundary_before_max",
            )
        end_sentence = min(after_target, key=lambda s: float(s["end"]))

    end = float(end_sentence["end"])
    duration = end - start
    if duration < limits["min"] - 1e-6 or duration > limits["max"] + 1e-6:
        return _snap_to_word_boundary_fallback(
            candidate,
            transcript,
            product,
            role,
            confidence,
            start_hint,
            limits,
            target,
            words_all=words_all,
            start=start,
            reason=f"{role}_duration_outside_bounds",
        )

    words = _words_for_range(words_all, start, end)
    if not words:
        return None, "no_words"

    return _build_snapped_candidate(
        candidate,
        product,
        role,
        confidence,
        start,
        end,
        target,
        words,
        boundary_mode="sentence",
    ), None


def _snap_to_word_boundary_fallback(
    candidate: dict[str, Any],
    transcript: dict,
    product: str,
    role: str,
    confidence: float,
    start_hint: float,
    limits: dict[str, float],
    target: float,
    start: float | None = None,
    words_all: list[dict[str, Any]] | None = None,
    reason: str = "sentence_boundary_failed",
) -> tuple[dict[str, Any] | None, str | None]:
    words_all = words_all if words_all is not None else _normalized_transcript_words(transcript)
    if not words_all:
        return None, "no_words"

    if start is None:
        start_word = _word_for_start_hint(words_all, start_hint)
        if start_word is None:
            return None, reason
        start = float(start_word["start"])

    min_end = float(start) + limits["min"]
    max_end = float(start) + limits["max"]
    target_end = float(start) + target
    eligible = [
        word
        for word in words_all
        if float(word["end"]) >= min_end - 1e-6
        and float(word["end"]) <= max_end + 1e-6
        and float(word["end"]) > float(start) + 1e-6
    ]
    if not eligible:
        return None, f"{role}_duration_outside_bounds"

    end_word = min(eligible, key=lambda word: abs(float(word["end"]) - target_end))
    end = float(end_word["end"])
    duration = end - float(start)
    if duration < limits["min"] - 1e-6 or duration > limits["max"] + 1e-6:
        return None, f"{role}_duration_outside_bounds"

    words = _words_for_range(words_all, float(start), end)
    if not words:
        return None, "no_words"

    snapped = _build_snapped_candidate(
        candidate,
        product,
        role,
        confidence,
        float(start),
        end,
        target,
        words,
        boundary_mode="word_boundary_fallback",
    )
    snapped["sentence_boundary_failed_reason"] = reason
    return snapped, None


def _build_snapped_candidate(
    candidate: dict[str, Any],
    product: str,
    role: str,
    confidence: float,
    start: float,
    end: float,
    target: float,
    words: list[dict[str, Any]],
    boundary_mode: str,
) -> dict[str, Any]:
    source_moment_id = _matching_source_moment(candidate, start, end)
    transcript_text = " ".join(str(w.get("word", "")).strip() for w in words if str(w.get("word", "")).strip())
    return {
        "product": product,
        "role": role,
        "start": round(start, 6),
        "end": round(end, 6),
        "duration": round(end - start, 6),
        "target_duration": round(target, 6),
        "transcript_text": transcript_text,
        "classification_reason": str(candidate.get("classification_reason") or candidate.get("reason") or "").strip(),
        "suggested_hook": str(candidate.get("suggested_hook") or "").strip(),
        "confidence": round(confidence, 4),
        "source_moment_id": source_moment_id,
        "boundary_mode": boundary_mode,
        "words": _relative_words(words, start),
        "raw_candidate": candidate,
    }


def build_sentence_boundaries(
    transcript: dict,
    cfg,
    words: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    words = words if words is not None else _normalized_transcript_words(transcript)
    if not words:
        return []

    pause_threshold = float(getattr(cfg, "MODULE_SENTENCE_PAUSE_THRESHOLD", 0.7) or 0.7)
    end_indices = set()
    for index, word in enumerate(words):
        token = str(word.get("word", "")).strip()
        if SENTENCE_END_RE.search(token):
            end_indices.add(index)

    for segment in transcript.get("segments", []) or []:
        if not isinstance(segment, dict):
            continue
        seg_start = _safe_float(segment.get("start"), None)
        seg_end = _safe_float(segment.get("end"), None)
        if seg_start is None or seg_end is None:
            continue
        indices = [
            index
            for index, word in enumerate(words)
            if float(word["start"]) >= seg_start - 0.15 and float(word["end"]) <= seg_end + 0.25
        ]
        if not indices:
            continue
        last_index = indices[-1]
        if last_index >= len(words) - 1:
            end_indices.add(last_index)
            continue
        next_start = float(words[last_index + 1]["start"])
        if next_start - float(words[last_index]["end"]) >= pause_threshold:
            end_indices.add(last_index)
        if SENTENCE_END_RE.search(str(segment.get("text", "")).strip()):
            end_indices.add(last_index)

    if not end_indices or max(end_indices) != len(words) - 1:
        end_indices.add(len(words) - 1)

    sentences = []
    start_index = 0
    for end_index in sorted(end_indices):
        if end_index < start_index:
            continue
        sentence_words = words[start_index : end_index + 1]
        if not sentence_words:
            continue
        text = " ".join(word["word"] for word in sentence_words).strip()
        if text:
            sentences.append(
                {
                    "start": round(float(sentence_words[0]["start"]), 6),
                    "end": round(float(sentence_words[-1]["end"]), 6),
                    "start_index": start_index,
                    "end_index": end_index,
                    "text": text,
                }
            )
        start_index = end_index + 1

    return sentences


def transcript_context_text(
    transcript: dict,
    start: float,
    end: float,
    cfg,
    words: list[dict[str, Any]] | None = None,
) -> str:
    padding = float(getattr(cfg, "MODULE_PRODUCT_EVIDENCE_CONTEXT_SECONDS", 12.0) or 0.0)
    window_start = max(0.0, float(start) - padding)
    window_end = float(end) + padding
    words = words if words is not None else _normalized_transcript_words(transcript)
    words = [
        word
        for word in words
        if float(word.get("end", 0.0)) >= window_start and float(word.get("start", 0.0)) <= window_end
    ]
    if words:
        return " ".join(str(word.get("word", "")).strip() for word in words if str(word.get("word", "")).strip())

    segments = []
    for segment in transcript.get("segments", []) or []:
        if not isinstance(segment, dict):
            continue
        seg_start = _safe_float(segment.get("start"), None)
        seg_end = _safe_float(segment.get("end"), None)
        if seg_start is None or seg_end is None:
            continue
        if seg_end >= window_start and seg_start <= window_end:
            text = str(segment.get("text") or "").strip()
            if text:
                segments.append(text)
    return " ".join(segments)


def _normalized_transcript_words(transcript: dict) -> list[dict[str, Any]]:
    words = [
        {
            "word": str(word.get("word", "")).strip(),
            "start": _safe_float(word.get("start"), 0.0),
            "end": _safe_float(word.get("end"), 0.0),
        }
        for word in transcript.get("words", []) or []
        if isinstance(word, dict) and str(word.get("word", "")).strip()
    ]
    words = [word for word in words if float(word["end"]) >= float(word["start"])]
    words.sort(key=lambda item: (float(item["start"]), float(item["end"])))
    return words


def cut_and_register_module(
    candidate: dict[str, Any],
    video_path: str,
    output_path: Path,
    cfg,
    force: bool = False,
) -> dict[str, Any]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = output_path.with_suffix(output_path.suffix + ".lock")

    with module_file_lock(lock_path, cfg):
        existing_probe = probe_media(output_path) if output_path.exists() else None
        sidecar_path = module_sidecar_path(output_path)
        existing_record = _read_json_file(sidecar_path)
        if output_path.exists() and existing_record and not force:
            if not _sidecar_matches_candidate(existing_record, candidate, output_path, video_path, cfg):
                raise ModuleExtractionError(
                    f"deterministic module filename collision: {output_path}",
                    "filename_collision",
                )
        if existing_probe and _probe_matches_candidate(existing_probe, candidate, cfg):
            if not force:
                if existing_record:
                    record = dict(existing_record)
                    record.update(module_quality_fields(record, cfg))
                    record["status"] = "skipped_existing"
                else:
                    record = _build_sidecar_record(candidate, video_path, output_path, existing_probe, status="skipped_existing", cfg=cfg)
                    _write_json_atomic(sidecar_path, record)
                return record

        source_visual_result: dict[str, Any] | None = None
        if bool(getattr(cfg, "MODULE_VALIDATE_ON_EXTRACT", False)):
            try:
                from module_visual_validator import scan_source_video_window_visual_events

                source_visual_result = scan_source_video_window_visual_events(
                    video_path=video_path,
                    product=str(candidate.get("product") or ""),
                    start=float(candidate["start"]),
                    end=float(candidate["end"]),
                    cfg=cfg,
                )
            except Exception as exc:
                log.warning("Source-VOD visual validation skipped for %s: %s", output_path, exc)
                source_visual_result = {
                    "status": "not_run",
                    "reason": f"source_vod_validator_error:{exc}",
                    "events": [],
                    "hits": 0,
                    "confidence_max": 0.0,
                }

        ok = cut_module_reencode(video_path, candidate["start"], candidate["end"], output_path, cfg)
        if not ok:
            raise ModuleExtractionError("ffmpeg module cut failed", "ffmpeg_cut_failed")

        probe = probe_media(output_path)
        if not probe:
            raise ModuleExtractionError("ffprobe could not validate module", "ffprobe_failed")
        if not _probe_matches_candidate(probe, candidate, cfg):
            raise ModuleExtractionError("module failed duration/audio/video validation", "validation_failed")

        record = _build_sidecar_record(candidate, video_path, output_path, probe, status="created", cfg=cfg)
        if bool(getattr(cfg, "MODULE_VALIDATE_ON_EXTRACT", False)):
            try:
                from module_visual_validator import apply_source_vod_visual_result

                record = apply_source_vod_visual_result(record, source_visual_result, cfg)
            except Exception as exc:
                log.warning("Source-VOD visual validation annotation skipped for %s: %s", output_path, exc)
                record["visual_validation_status"] = "not_run"
                record["visual_validation_reason"] = f"validator_error:{exc}"
        _write_json_atomic(module_sidecar_path(output_path), record)
        return record


def module_output_path(library_dir: Path, candidate: dict[str, Any], source_video: Path, cfg) -> Path:
    source_date = source_video_date(source_video)
    start_ms = int(round(float(candidate["start"]) * 1000))
    filename = f"{candidate['product']}_{candidate['role']}_{source_date}_{start_ms}.mp4"
    return library_dir / candidate["product"] / candidate["role"] / filename


def module_sidecar_path(module_path: Path) -> Path:
    return module_path.with_suffix(".json")


def read_library_index(cfg) -> dict[str, Any]:
    library = Path(getattr(cfg, "MODULE_LIBRARY_DIR", r"D:\proya_modules"))
    _ensure_library_layout(library)
    with library_index_lock(library, cfg):
        return _read_index_snapshot_or_rebuild(library, cfg)


def _read_index_snapshot_or_rebuild(library: Path, cfg) -> dict[str, Any]:
    index_path = library / "index.json"
    if index_path.exists():
        try:
            payload = json.loads(index_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and isinstance(payload.get("modules"), list):
                return payload
        except Exception as exc:
            log.warning("Ignoring unreadable module index %s: %s", index_path, exc)
    return rebuild_library_index(library, cfg, write=True)


def rebuild_library_index(library_dir: Path, cfg, write: bool = False) -> dict[str, Any]:
    modules = []
    for product in PRODUCT_FOLDERS:
        for role in ROLE_FOLDERS:
            folder = library_dir / product / role
            if not folder.exists():
                continue
            for sidecar in sorted(folder.glob("*.json")):
                if sidecar.name == "index.json":
                    continue
                try:
                    record = json.loads(sidecar.read_text(encoding="utf-8"))
                except Exception as exc:
                    log.warning("Ignoring unreadable module sidecar %s: %s", sidecar, exc)
                    continue
                if _index_record_is_valid(record, cfg):
                    modules.append(_index_summary(record, cfg))

    modules.sort(key=lambda item: (item.get("product", ""), item.get("role", ""), item.get("source_video", ""), item.get("start", 0.0)))
    index = {
        "schema_version": MODULE_SCHEMA_VERSION,
        "updated_at": _now_iso(),
        "library_dir": str(library_dir.resolve()),
        "module_count": len(modules),
        "modules": modules,
    }
    if write:
        _write_json_atomic(library_dir / "index.json", index)
    return index


def _record_media_exists(record: dict[str, Any]) -> bool:
    if not isinstance(record, dict):
        return False
    path = record.get("file_path")
    return bool(path) and Path(str(path)).exists()


@contextmanager
def library_index_lock(library_dir: Path, cfg):
    library_dir.mkdir(parents=True, exist_ok=True)
    timeout = float(getattr(cfg, "MODULE_INDEX_LOCK_TIMEOUT", 30.0) or 30.0)
    lock_path = library_dir / "index.json.lock"
    with _portalocker_lock_with_timeout(lock_path, timeout, "module index"):
        yield


@contextmanager
def module_file_lock(lock_path: Path, cfg):
    timeout = float(getattr(cfg, "MODULE_FILE_LOCK_TIMEOUT", 30.0) or 30.0)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _portalocker_lock_with_timeout(lock_path, timeout, "module file"):
        yield


@contextmanager
def _portalocker_lock_with_timeout(lock_path: Path, timeout: float, label: str):
    portalocker = _require_portalocker()
    deadline = time.monotonic() + max(0.0, float(timeout))
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    acquired = False
    try:
        while True:
            try:
                portalocker.lock(handle, portalocker.LOCK_EX | portalocker.LOCK_NB)
                acquired = True
                break
            except portalocker.exceptions.LockException as exc:
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"Could not acquire {label} lock within {timeout:.0f}s: {lock_path}") from exc
                time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        yield
    finally:
        if acquired:
            portalocker.unlock(handle)
        handle.close()


def canonical_product(value: Any) -> str | None:
    text = _normalize_text(value)
    if not text or text in {"general", "unknown", "tidak jelas", "produk"}:
        return None
    for product, aliases in PRODUCT_ALIASES.items():
        if text == product or text.replace(" ", "_") == product:
            return product
        for alias in aliases:
            alias_norm = _normalize_text(alias)
            if alias_norm and alias_norm in text:
                return product
    return None


def canonical_role(value: Any) -> str | None:
    text = _normalize_text(value)
    for role, aliases in ROLE_ALIASES.items():
        if text == role or any(alias in text for alias in aliases):
            return role
    return None


def product_has_transcript_evidence(product: str, candidate: dict[str, Any], cfg) -> bool:
    if not bool(getattr(cfg, "MODULE_PRODUCT_EVIDENCE_REQUIRED", True)):
        return True
    raw_candidate = candidate.get("raw_candidate") if isinstance(candidate.get("raw_candidate"), dict) else {}
    words = candidate.get("words") if isinstance(candidate.get("words"), list) else []
    raw_words = raw_candidate.get("words") if isinstance(raw_candidate.get("words"), list) else []
    evidence_text = " ".join(
        str(value or "")
        for value in (
            candidate.get("transcript_text"),
            candidate.get("evidence_context_text"),
            raw_candidate.get("transcript_text"),
            " ".join(str(word.get("word", "")) for word in words if isinstance(word, dict)),
            " ".join(str(word.get("word", "")) for word in raw_words if isinstance(word, dict)),
        )
    )
    normalized = _normalize_text(evidence_text)
    if not normalized:
        return False
    aliases = PRODUCT_ALIASES.get(product, ())
    terms = {_normalize_text(product), _normalize_text(product.replace("_", " "))}
    terms.update(_normalize_text(alias) for alias in aliases)
    return any(term and term in normalized for term in terms)


def module_quality_fields(record: dict[str, Any], cfg) -> dict[str, Any]:
    boundary_mode = str(record.get("boundary_mode") or "sentence")
    review_status = str(record.get("review_status") or REVIEW_PENDING).strip() or REVIEW_PENDING
    explicit_status = str(record.get("quality_status") or "").strip()
    try:
        schema_version = int(record.get("schema_version") or 0)
    except (TypeError, ValueError):
        schema_version = 0
    explicit_is_authoritative = explicit_status in {QUALITY_APPROVED, QUALITY_NEEDS_REVIEW, QUALITY_BLOCKED, QUALITY_NO_VISUAL_EVENTS} and (
        schema_version >= MODULE_SCHEMA_VERSION or explicit_status != QUALITY_APPROVED
    )

    if review_status == QUALITY_APPROVED:
        quality_status = QUALITY_APPROVED
        quality_reason = str(record.get("quality_reason") or "manual_review_approved")
    elif explicit_is_authoritative:
        quality_status = explicit_status
        quality_reason = str(record.get("quality_reason") or explicit_status)
    elif boundary_mode == "word_boundary_fallback" and bool(getattr(cfg, "MODULE_WORD_FALLBACK_REVIEW_REQUIRED", True)):
        quality_status = QUALITY_NEEDS_REVIEW
        quality_reason = "word_boundary_fallback_requires_review"
    elif record.get("product") and not product_has_transcript_evidence(str(record.get("product")), record, cfg):
        quality_status = QUALITY_NEEDS_REVIEW
        quality_reason = "product_evidence_unverified"
    else:
        quality_status = QUALITY_APPROVED
        quality_reason = "sentence_boundary_validated"

    try:
        quality_score = float(record.get("quality_score"))
    except (TypeError, ValueError):
        quality_score = _computed_quality_score(record, quality_status)

    return {
        "quality_status": quality_status,
        "quality_reason": quality_reason,
        "quality_score": round(quality_score, 3),
        "review_status": review_status,
    }


def _computed_quality_score(record: dict[str, Any], quality_status: str) -> float:
    confidence = _safe_float(record.get("confidence"), 0.0) or 0.0
    score = max(0.0, min(10.0, confidence * 10.0))
    if str(record.get("boundary_mode") or "") == "word_boundary_fallback":
        score -= 1.5
    if quality_status == QUALITY_NEEDS_REVIEW:
        score -= 0.5
    if quality_status == QUALITY_BLOCKED:
        score = 0.0
    return max(0.0, round(score, 3))


def role_duration_limits(role: str, cfg) -> dict[str, float]:
    strict = bool(getattr(cfg, "MODULE_DURATION_STRICT", False))
    if role == "hook":
        fallback_min, fallback_max = (5.0, 7.0) if strict else (4.0, 8.0)
        return {
            "min": float(getattr(cfg, "MODULE_HOOK_MIN_DURATION", fallback_min) or fallback_min) if not strict else fallback_min,
            "max": float(getattr(cfg, "MODULE_HOOK_MAX_DURATION", fallback_max) or fallback_max) if not strict else fallback_max,
            "default": 6.0,
        }
    if role == "main":
        fallback_min, fallback_max = (20.0, 40.0) if strict else (15.0, 45.0)
        return {
            "min": float(getattr(cfg, "MODULE_MAIN_MIN_DURATION", fallback_min) or fallback_min) if not strict else fallback_min,
            "max": float(getattr(cfg, "MODULE_MAIN_MAX_DURATION", fallback_max) or fallback_max) if not strict else fallback_max,
            "default": 30.0,
        }
    fallback_min, fallback_max = (5.0, 10.0) if strict else (4.0, 12.0)
    max_duration = float(getattr(cfg, "MODULE_CTA_MAX_DURATION", fallback_max) or fallback_max) if not strict else fallback_max
    return {
        "min": float(getattr(cfg, "MODULE_CTA_MIN_DURATION", fallback_min) or fallback_min) if not strict else fallback_min,
        "max": max_duration,
        "default": min(7.0, max_duration),
    }


def cut_module_reencode(video_path: str, start: float, end: float, output_path: Path, cfg) -> bool:
    duration = max(0.0, float(end) - float(start))
    timeout = max(
        30,
        int(duration * 6) + 60,
        int(getattr(cfg, "MODULE_EXTRACT_FFMPEG_TIMEOUT", 300) or 300),
    )
    video_encoder = "libx264"
    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        f"{float(start):.3f}",
        "-to",
        f"{float(end):.3f}",
        "-i",
        video_path,
        "-c:v",
        video_encoder,
        "-preset",
        "fast",
        "-crf",
        "18",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-avoid_negative_ts",
        "make_zero",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        stderr = _timeout_stream_text(exc.stderr)
        log.error(
            "ffmpeg module cut timed out after %ss using CPU encoder %s: %s\nstderr:\n%s",
            timeout,
            video_encoder,
            shlex.join(cmd),
            stderr or "(no stderr captured before timeout)",
        )
        raise
    if result.returncode != 0:
        log.warning("ffmpeg module cut failed: %s", (result.stderr or "").strip()[-1000:])
        return False
    return output_path.exists() and output_path.stat().st_size > 0


def _timeout_stream_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def probe_media(path: str | Path) -> dict[str, Any] | None:
    media_path = Path(path)
    if not media_path.exists():
        return None
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=index,codec_type,codec_name,width,height,sample_rate",
        "-of",
        "json",
        str(media_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return None

    streams = payload.get("streams", []) or []
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    duration = _safe_float((payload.get("format") or {}).get("duration"), None)
    return {
        "duration": duration,
        "has_video": video is not None,
        "has_audio": audio is not None,
        "video_codec": video.get("codec_name") if video else None,
        "audio_codec": audio.get("codec_name") if audio else None,
        "width": int(video.get("width") or 0) if video else None,
        "height": int(video.get("height") or 0) if video else None,
        "audio_sample_rate": int(audio.get("sample_rate") or 0) if audio and audio.get("sample_rate") else None,
    }


def source_date_from_source_video(source_video: str | Path | None) -> str:
    if not source_video:
        return ""
    filename = re.split(r"[\\/]", str(source_video).strip())[-1]
    match = VOD_SOURCE_FILENAME_RE.match(filename)
    if not match:
        return ""
    try:
        datetime.strptime(filename[:-4], "%Y-%m-%d-%H-%M-%S")
    except ValueError:
        return ""
    return match.group("date")


def source_video_date(source_video: Path) -> str:
    source_date = source_date_from_source_video(source_video)
    if source_date:
        return source_date.replace("-", "")
    match = re.search(r"(20\d{2})[-_]?([01]\d)[-_]?([0-3]\d)", source_video.stem)
    if match:
        return "".join(match.groups())
    try:
        return datetime.fromtimestamp(source_video.stat().st_mtime).strftime("%Y%m%d")
    except OSError:
        return datetime.now().strftime("%Y%m%d")


class ModuleExtractionError(RuntimeError):
    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def _load_or_classify_candidates(source: Path, transcript: dict, working_dir: Path, cfg) -> list[dict[str, Any]]:
    cache_path = working_dir / "module_candidates.json"
    source_identity = _path_identity(source)
    cache_key = _candidate_cache_key(source_identity, cfg)
    if bool(getattr(cfg, "MODULE_CANDIDATE_CACHE_ENABLED", True)) and cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached.get("cache_key") == cache_key and isinstance(cached.get("candidates"), list):
                log.info("Loading cached module candidates from %s", cache_path)
                policy_hash = str(cached.get("extraction_policy_hash") or "")
                for candidate in cached["candidates"]:
                    if isinstance(candidate, dict):
                        candidate["_cached_extraction_policy_hash"] = policy_hash
                return cached["candidates"]
        except Exception as exc:
            log.warning("Ignoring unreadable module candidate cache %s: %s", cache_path, exc)

    from transcriber import build_text_chunks

    chunk_duration = float(getattr(cfg, "MODULE_CHUNK_DURATION", getattr(cfg, "CHUNK_DURATION", 300)) or 300)
    chunk_overlap = float(getattr(cfg, "MODULE_CHUNK_OVERLAP", getattr(cfg, "CHUNK_OVERLAP", 45)) or 45)
    chunks = build_text_chunks(transcript, chunk_duration, chunk_overlap)
    candidates = classify_module_candidates(chunks, cfg)
    payload = {
        "schema_version": MODULE_SCHEMA_VERSION,
        "extractor_version": MODULE_EXTRACTOR_VERSION,
        "cache_key": cache_key,
        "candidate_cache_key": cache_key,
        "extraction_policy": _extraction_policy(cfg),
        "extraction_policy_hash": _extraction_policy_hash(cfg),
        "source_video_identity": source_identity,
        "created_at": _now_iso(),
        "candidates": candidates,
    }
    _write_json_atomic(cache_path, payload)
    return candidates


def _write_candidate_cache(source: Path, working_dir: Path, cfg, candidates: list[dict[str, Any]]) -> None:
    cache_path = working_dir / "module_candidates.json"
    source_identity = _path_identity(source)
    cache_key = _candidate_cache_key(source_identity, cfg)
    payload = {}
    if cache_path.exists():
        try:
            loaded = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                payload = loaded
        except Exception:
            payload = {}
    payload.update(
        {
            "schema_version": MODULE_SCHEMA_VERSION,
            "extractor_version": MODULE_EXTRACTOR_VERSION,
            "cache_key": cache_key,
            "candidate_cache_key": cache_key,
            "extraction_policy": _extraction_policy(cfg),
            "extraction_policy_hash": _extraction_policy_hash(cfg),
            "source_video_identity": source_identity,
            "updated_at": _now_iso(),
            "candidates": [_candidate_cache_record(candidate) for candidate in candidates],
        }
    )
    payload.setdefault("created_at", _now_iso())
    _write_json_atomic(cache_path, payload)


def _classify_chunk(client, index: int, chunk: dict, cfg) -> list[dict[str, Any]]:
    log.info(
        "  Module chunk %s | t=%.0fs-%.0fs",
        index + 1,
        float(chunk.get("chunk_start", 0.0)),
        float(chunk.get("chunk_end", 0.0)),
    )
    model_id = getattr(cfg, "LM_STUDIO_MODEL", "")
    response = client.chat.completions.create(
        model=model_id,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(chunk)},
        ],
        max_tokens=int(getattr(cfg, "MODULE_CLASSIFIER_MAX_TOKENS", 8192) or 8192),
        timeout=float(getattr(cfg, "LM_STUDIO_TIMEOUT", 360) or 360),
        **lm_studio_openai_chat_kwargs(
            cfg,
            model_id=model_id,
            temperature=float(getattr(cfg, "MODULE_CLASSIFIER_TEMPERATURE", 0.2) or 0.2),
        ),
    )
    raw = response.choices[0].message.content.strip()
    candidates = parse_module_candidates_json(raw)
    for candidate in candidates:
        if isinstance(candidate, dict):
            candidate.setdefault("_chunk_start", chunk.get("chunk_start"))
            candidate.setdefault("_chunk_end", chunk.get("chunk_end"))
    return [candidate for candidate in candidates if isinstance(candidate, dict)]


def _build_user_prompt(chunk: dict) -> str:
    return (
        "Ini transkrip livestream PROYA.\n"
        f"Rentang waktu: {float(chunk.get('chunk_start', 0.0)):.1f}s sampai {float(chunk.get('chunk_end', 0.0)):.1f}s.\n\n"
        f"{chunk.get('text', '')}\n\n"
        "Cari kandidat modul hook, main, dan cta yang kuat. Return JSON array saja."
    )


def parse_module_candidates_json(raw: str) -> list[dict[str, Any]]:
    cleaned = re.sub(r"```(?:json)?", "", raw or "", flags=re.IGNORECASE).strip()
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\[[\s\S]*\]", cleaned)
        if not match:
            return []
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []
    if isinstance(payload, dict):
        payload = payload.get("candidates") or payload.get("modules") or payload.get("items") or []
    return payload if isinstance(payload, list) else []


def _candidate_cache_key(source_identity: dict[str, Any], cfg) -> str:
    values = {
        "source": source_identity,
        "extractor_version": MODULE_EXTRACTOR_VERSION,
        "lm_model": getattr(cfg, "LM_STUDIO_MODEL", ""),
        "chunk_duration": getattr(cfg, "MODULE_CHUNK_DURATION", getattr(cfg, "CHUNK_DURATION", None)),
        "chunk_overlap": getattr(cfg, "MODULE_CHUNK_OVERLAP", getattr(cfg, "CHUNK_OVERLAP", None)),
        "products": PRODUCT_FOLDERS,
        "roles": ROLE_FOLDERS,
    }
    raw = json.dumps(values, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _extraction_policy(cfg) -> dict[str, Any]:
    return {
        "duration_strict": bool(getattr(cfg, "MODULE_DURATION_STRICT", False)),
        "hook_duration": [
            getattr(cfg, "MODULE_HOOK_MIN_DURATION", None),
            getattr(cfg, "MODULE_HOOK_MAX_DURATION", None),
        ],
        "main_duration": [
            getattr(cfg, "MODULE_MAIN_MIN_DURATION", None),
            getattr(cfg, "MODULE_MAIN_MAX_DURATION", None),
        ],
        "cta_duration": [
            getattr(cfg, "MODULE_CTA_MIN_DURATION", None),
            getattr(cfg, "MODULE_CTA_MAX_DURATION", None),
        ],
        "sentence_boundary_tolerance": getattr(cfg, "MODULE_SENTENCE_BOUNDARY_TOLERANCE", None),
        "word_fallback_review_required": bool(getattr(cfg, "MODULE_WORD_FALLBACK_REVIEW_REQUIRED", True)),
        "product_evidence_required": bool(getattr(cfg, "MODULE_PRODUCT_EVIDENCE_REQUIRED", True)),
        "product_evidence_context_seconds": getattr(cfg, "MODULE_PRODUCT_EVIDENCE_CONTEXT_SECONDS", None),
        "extractor_version": MODULE_EXTRACTOR_VERSION,
    }


def _extraction_policy_hash(cfg) -> str:
    raw = json.dumps(_extraction_policy(cfg), sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _sentence_for_start_hint(sentences: list[dict[str, Any]], start_hint: float) -> dict[str, Any] | None:
    containing = [s for s in sentences if float(s["start"]) <= start_hint <= float(s["end"])]
    if containing:
        return containing[0]
    after = [s for s in sentences if float(s["start"]) >= start_hint]
    if after:
        return min(after, key=lambda s: float(s["start"]))
    before = [s for s in sentences if float(s["end"]) >= start_hint - 2.0]
    return before[-1] if before else None


def _word_for_start_hint(words: list[dict[str, Any]], start_hint: float) -> dict[str, Any] | None:
    containing = [w for w in words if float(w["start"]) <= start_hint <= float(w["end"])]
    if containing:
        return containing[0]
    after = [w for w in words if float(w["start"]) >= start_hint]
    if after:
        return min(after, key=lambda w: float(w["start"]))
    before = [w for w in words if float(w["end"]) >= start_hint - 2.0]
    return before[-1] if before else None


def _target_duration(candidate: dict[str, Any], role: str, limits: dict[str, float]) -> float:
    target = _safe_float(candidate.get("target_duration"), None)
    if target is None:
        start = _safe_float(candidate.get("start_hint", candidate.get("start")), None)
        end = _safe_float(candidate.get("end_hint", candidate.get("end")), None)
        if start is not None and end is not None and end > start:
            target = end - start
    if target is None:
        target = limits["default"]
    return min(limits["max"], max(limits["min"], float(target)))


def _matching_source_moment(candidate: dict[str, Any], start: float, end: float) -> str:
    explicit = candidate.get("source_moment_id") or candidate.get("clip_id")
    if explicit:
        return str(explicit)
    return ""


def _words_for_range(words: list, start: float, end: float) -> list[dict[str, Any]]:
    selected = []
    for word in words or []:
        if not isinstance(word, dict):
            continue
        word_start = _safe_float(word.get("start"), None)
        word_end = _safe_float(word.get("end"), None)
        if word_start is None or word_end is None:
            continue
        if word_start >= start - 1e-6 and word_end <= end + 1e-6:
            selected.append({"word": str(word.get("word", "")).strip(), "start": word_start, "end": word_end})
    return [word for word in selected if word["word"]]


def _relative_words(words: list[dict[str, Any]], start: float) -> list[dict[str, Any]]:
    return [
        {
            "word": word["word"],
            "start": round(float(word["start"]) - start, 6),
            "end": round(float(word["end"]) - start, 6),
        }
        for word in words
    ]


def _find_duplicate_module(
    candidate: dict[str, Any],
    records: list[dict[str, Any]],
    output_path: Path,
    force: bool,
    cfg,
) -> dict[str, Any] | None:
    threshold = float(getattr(cfg, "MODULE_DEDUPE_IOU_THRESHOLD", 0.5) or 0.5)
    source_key = _source_identity_key(candidate.get("source_video_identity"))
    source_video = str(candidate.get("source_video", "")).casefold()
    for record in records:
        record_path = Path(str(record.get("file_path") or ""))
        if force and record_path and _same_path(record_path, output_path):
            continue
        record_source_key = _source_identity_key(record.get("source_video_identity"))
        record_source = str(record.get("source_video", "")).casefold()
        same_source = bool(source_key and source_key == record_source_key) or bool(source_video and source_video == record_source)
        if not same_source:
            continue
        overlap = _temporal_iou(
            float(candidate.get("start", 0.0)),
            float(candidate.get("end", 0.0)),
            float(record.get("start", 0.0)),
            float(record.get("end", 0.0)),
        )
        if overlap > threshold:
            return record
    return None


def _temporal_iou(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    intersection = max(0.0, min(a_end, b_end) - max(a_start, b_start))
    union = max(a_end, b_end) - min(a_start, b_start)
    return intersection / union if union > 0 else 0.0


def _read_json_file(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _sidecar_matches_candidate(
    record: dict[str, Any],
    candidate: dict[str, Any],
    output_path: Path,
    video_path: str,
    cfg,
) -> bool:
    if record.get("product") != candidate.get("product") or record.get("role") != candidate.get("role"):
        return False
    if not _same_path(Path(str(record.get("file_path") or output_path)), output_path):
        return False
    for key in ("start", "end", "duration"):
        left = _safe_float(record.get(key), None)
        right = _safe_float(candidate.get(key), None)
        if left is None or right is None or abs(left - right) > 0.01:
            return False

    record_identity = _source_identity_key(record.get("source_video_identity"))
    candidate_identity = _source_identity_key(candidate.get("source_video_identity"))
    if record_identity and candidate_identity:
        return record_identity == candidate_identity

    record_source = str(record.get("source_video") or "")
    if record_source:
        return _same_path(Path(record_source), Path(video_path))
    return True


def _build_sidecar_record(
    candidate: dict[str, Any],
    video_path: str,
    output_path: Path,
    probe: dict[str, Any],
    status: str,
    cfg,
) -> dict[str, Any]:
    source = Path(video_path)
    source_date = source_date_from_source_video(source)
    module_id = output_path.stem
    record = {
        "schema_version": MODULE_SCHEMA_VERSION,
        "extractor_version": MODULE_EXTRACTOR_VERSION,
        "module_id": module_id,
        "status": status,
        "product": candidate["product"],
        "role": candidate["role"],
        "source_video": str(source.resolve()),
        "source_video_identity": _path_identity(source),
        "source_date": source_date,
        "source_video_date": source_video_date(source),
        "source_moment_id": candidate.get("source_moment_id", ""),
        "file_path": str(output_path.resolve()),
        "start": round(float(candidate["start"]), 6),
        "end": round(float(candidate["end"]), 6),
        "duration": round(float(candidate["duration"]), 6),
        "target_duration": round(float(candidate.get("target_duration", candidate["duration"])), 6),
        "transcript_text": candidate.get("transcript_text", ""),
        "classification_reason": candidate.get("classification_reason", ""),
        "suggested_hook": candidate.get("suggested_hook", ""),
        "evidence_context_text": candidate.get("evidence_context_text", ""),
        "confidence": float(candidate.get("confidence", 0.0)),
        "boundary_mode": candidate.get("boundary_mode", "sentence"),
        "sentence_boundary_failed_reason": candidate.get("sentence_boundary_failed_reason", ""),
        "visual_validation_status": "not_run",
        "visual_product_hits": 0,
        "visual_product_confidence_max": 0.0,
        "visual_validation_reason": "not_enabled",
        "visual_validation_mode": "",
        "visual_product_events": [],
        "created_at": _now_iso(),
        "words": candidate.get("words", []),
        "ffprobe": probe,
    }
    record.update(module_quality_fields(record, cfg))
    return record


def _probe_matches_candidate(probe: dict[str, Any], candidate: dict[str, Any], cfg) -> bool:
    if not probe.get("has_video") or not probe.get("has_audio"):
        return False
    duration = _safe_float(probe.get("duration"), None)
    if duration is None:
        return False
    planned = float(candidate.get("duration", 0.0))
    if abs(duration - planned) > 1.0:
        return False
    limits = role_duration_limits(candidate.get("role", ""), cfg)
    return limits["min"] <= duration <= limits["max"]


def _index_record_is_valid(record: dict[str, Any], cfg) -> bool:
    if not isinstance(record, dict):
        return False
    try:
        schema_version = int(record.get("schema_version") or 0)
    except (TypeError, ValueError):
        return False
    if schema_version not in SUPPORTED_MODULE_SCHEMA_VERSIONS:
        return False
    if record.get("product") not in PRODUCT_FOLDERS or record.get("role") not in ROLE_FOLDERS:
        return False
    path = Path(str(record.get("file_path") or ""))
    if not path.exists():
        return False
    if bool(getattr(cfg, "MODULE_INDEX_VALIDATE_MEDIA", True)):
        if bool(getattr(cfg, "MODULE_INDEX_REPROBE_MEDIA", False)):
            probe = probe_media(path)
        else:
            probe = record.get("ffprobe")
        if not probe or not _probe_matches_candidate(probe, record, cfg):
            return False
    return True


def _index_summary(record: dict[str, Any], cfg) -> dict[str, Any]:
    quality = module_quality_fields(record, cfg)
    summary = {
        key: record.get(key)
        for key in (
            "schema_version",
            "module_id",
            "product",
            "role",
            "source_video",
            "source_video_identity",
            "source_date",
            "source_video_date",
            "source_moment_id",
            "file_path",
            "start",
            "end",
            "duration",
            "transcript_text",
            "classification_reason",
            "suggested_hook",
            "evidence_context_text",
            "confidence",
            "boundary_mode",
            "sentence_boundary_failed_reason",
            "quality_status",
            "quality_reason",
            "quality_score",
            "review_status",
            "visual_validation_status",
            "visual_product_hits",
            "visual_product_confidence_max",
            "visual_validation_reason",
            "visual_validation_mode",
            "visual_validation_fingerprint",
            "created_at",
            "ffprobe",
        )
    }
    summary["source_date"] = summary.get("source_date") or source_date_from_source_video(record.get("source_video"))
    summary["visual_validation_status"] = summary.get("visual_validation_status") or "not_run"
    summary["visual_product_hits"] = int(summary.get("visual_product_hits") or 0)
    summary["visual_product_confidence_max"] = float(summary.get("visual_product_confidence_max") or 0.0)
    summary["visual_validation_reason"] = summary.get("visual_validation_reason") or ""
    summary["visual_validation_mode"] = summary.get("visual_validation_mode") or ""
    return summary | quality | {"sidecar_path": str(module_sidecar_path(Path(str(record.get("file_path")))).resolve())}


def _ensure_library_layout(library_dir: Path) -> None:
    for product in PRODUCT_FOLDERS:
        for role in ROLE_FOLDERS:
            (library_dir / product / role).mkdir(parents=True, exist_ok=True)


def _empty_result(candidate_count: int, reason: str) -> dict[str, Any]:
    return {
        "candidates": candidate_count,
        "accepted": 0,
        "skipped_existing": 0,
        "skipped_duplicate": 0,
        "rejected": candidate_count,
        "rejected_pre_cut": candidate_count,
        "failed_post_cut": 0,
        "rejected_total": candidate_count,
        "failed": 0,
        "skipped_previous_failure": 0,
        "skipped_candidate_cap": 0,
        "word_boundary_fallback": 0,
        "modules": [],
        "reject_reasons": {_public_reject_reason(reason): candidate_count},
        "reject_details": {reason: candidate_count},
    }


def _new_stats(candidate_count: int) -> dict[str, Any]:
    return {
        "candidates": candidate_count,
        "accepted": 0,
        "skipped_existing": 0,
        "skipped_duplicate": 0,
        "rejected": 0,
        "rejected_pre_cut": 0,
        "failed_post_cut": 0,
        "rejected_total": 0,
        "failed": 0,
        "skipped_previous_failure": 0,
        "skipped_candidate_cap": 0,
        "word_boundary_fallback": 0,
        "modules": [],
        "reject_reasons": {},
        "reject_details": {},
    }


def _count_reject(stats: dict[str, Any], reason: str, phase: str = "pre_cut") -> None:
    public_reason = _public_reject_reason(reason)
    stats["rejected_total"] = stats.get("rejected_total", 0) + 1
    stats["rejected"] = stats["rejected_total"]
    if phase == "post_cut":
        stats["failed_post_cut"] = stats.get("failed_post_cut", 0) + 1
    else:
        stats["rejected_pre_cut"] = stats.get("rejected_pre_cut", 0) + 1
    stats["reject_reasons"][public_reason] = stats["reject_reasons"].get(public_reason, 0) + 1
    stats.setdefault("reject_details", {})
    stats["reject_details"][reason] = stats["reject_details"].get(reason, 0) + 1


def _reset_candidate_annotations(candidates: list[dict[str, Any]]) -> None:
    for index, candidate in enumerate(candidates or [], start=1):
        if not isinstance(candidate, dict):
            continue
        candidate["_previous_extraction_status"] = candidate.get("extraction_status")
        candidate["_previous_rejection_detail"] = candidate.get("rejection_detail")
        candidate["_previous_rejection_reason"] = candidate.get("rejection_reason")
        candidate.setdefault("candidate_id", f"candidate_{index:04d}")
        candidate["extraction_status"] = "pending"
        candidate["rejection_reason"] = None
        candidate["rejection_detail"] = None
        candidate["boundary_mode"] = None
        candidate["quality_status"] = None
        candidate["quality_reason"] = None
        candidate["quality_score"] = None
        candidate["review_status"] = None
        candidate["visual_validation_status"] = None
        candidate["visual_product_hits"] = None
        candidate["visual_product_confidence_max"] = None
        candidate["visual_validation_reason"] = None
        candidate["visual_validation_mode"] = None
        for key in ("module_id", "module_file", "module_sidecar", "module_status"):
            candidate.pop(key, None)


def _previous_post_cut_failure(candidate: dict[str, Any], cfg, force: bool = False) -> str | None:
    if force:
        return None
    if str(candidate.get("_cached_extraction_policy_hash") or "") != _extraction_policy_hash(cfg):
        return None
    if candidate.get("_previous_extraction_status") != "failed":
        return None
    reason = str(candidate.get("_previous_rejection_detail") or candidate.get("_previous_rejection_reason") or "")
    return reason if reason in POST_CUT_FAILURE_REASONS else None


def _candidate_cache_record(candidate: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(candidate, dict):
        return candidate
    return {
        key: value
        for key, value in candidate.items()
        if key not in INTERNAL_CANDIDATE_KEYS
    }


def _copy_snapped_annotation(candidate: dict[str, Any], snapped: dict[str, Any]) -> None:
    for key in (
        "start",
        "end",
        "duration",
        "target_duration",
        "transcript_text",
        "source_moment_id",
        "boundary_mode",
        "sentence_boundary_failed_reason",
        "evidence_context_text",
    ):
        if key in snapped:
            candidate[key] = snapped.get(key)


def _annotate_candidate(
    candidate: dict[str, Any],
    status: str,
    reason: str | None,
    record: dict[str, Any] | None = None,
) -> None:
    if not isinstance(candidate, dict):
        return
    candidate["extraction_status"] = status
    if reason:
        candidate["rejection_reason"] = _public_reject_reason(reason)
        candidate["rejection_detail"] = reason
        candidate["quality_status"] = QUALITY_BLOCKED
        candidate["quality_reason"] = reason
        candidate["quality_score"] = 0.0
        candidate["review_status"] = "rejected"
    else:
        candidate["rejection_reason"] = None
        candidate["rejection_detail"] = None
    if record:
        candidate["module_id"] = record.get("module_id")
        candidate["module_file"] = record.get("file_path")
        candidate["module_sidecar"] = str(module_sidecar_path(Path(str(record.get("file_path") or ""))))
        candidate["module_status"] = record.get("status")
        candidate["boundary_mode"] = record.get("boundary_mode")
        candidate["quality_status"] = record.get("quality_status")
        candidate["quality_reason"] = record.get("quality_reason")
        candidate["quality_score"] = record.get("quality_score")
        candidate["review_status"] = record.get("review_status")
        candidate["visual_validation_status"] = record.get("visual_validation_status", "not_run")
        candidate["visual_product_hits"] = record.get("visual_product_hits", 0)
        candidate["visual_product_confidence_max"] = record.get("visual_product_confidence_max", 0.0)
        candidate["visual_validation_reason"] = record.get("visual_validation_reason", "")
        candidate["visual_validation_mode"] = record.get("visual_validation_mode", "")


def _public_reject_reason(reason: str | None) -> str:
    text = str(reason or "other").strip() or "other"
    if text == "low_confidence":
        return "weak_confidence"
    if text == "no_words":
        return "no_spoken_words"
    if text.endswith("_duration_outside_bounds"):
        return "duration_out_of_range"
    if text.endswith("_no_boundary_before_max") or text in {
        "no_sentence_boundaries",
        "start_outside_transcript",
    }:
        return "sentence_boundary_failed"
    if text in {"invalid_candidate", ""}:
        return "other"
    return text


def _log_rejection_breakdown(stats: dict[str, Any]) -> None:
    reasons = stats.get("reject_reasons") or {}
    if not reasons:
        log.info("Rejected breakdown: none")
        return
    log.info("Rejected breakdown:")
    for reason, count in sorted(reasons.items(), key=lambda item: (-int(item[1]), str(item[0]))):
        log.info("  %-28s %5s", f"{reason}:", count)


def _require_portalocker():
    try:
        import portalocker
    except ImportError as exc:
        raise RuntimeError("portalocker is required for module extraction. Run: pip install portalocker") from exc
    return portalocker


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _safe_float(value: Any, default: float | None = 0.0) -> float | None:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_text(value: Any) -> str:
    text = str(value or "").casefold().replace("_", " ")
    text = re.sub(r"[^0-9a-zA-Z\u00c0-\u024f\u1e00-\u1eff\s]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _source_identity_key(identity: Any) -> str:
    if not isinstance(identity, dict):
        return ""
    return "|".join(str(identity.get(key, "")).casefold() for key in ("path", "size", "mtime_ns"))


def _same_path(left: Path, right: Path) -> bool:
    try:
        return str(left.resolve()).casefold() == str(right.resolve()).casefold()
    except OSError:
        return str(left).casefold() == str(right).casefold()


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
