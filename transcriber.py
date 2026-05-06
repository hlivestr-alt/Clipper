# =============================================================================
#  transcriber.py - Audio transcription via faster-whisper + WhisperX alignment
#  Produces segment text plus precise word-level timestamps for karaoke subtitles
# =============================================================================

import gc
import importlib.util
import json
import logging
import os
import re
import sys
import types
from pathlib import Path
from typing import Optional

log = logging.getLogger("proya.transcriber")

TRANSCRIPT_SCHEMA_VERSION = 3
RAW_TRANSCRIPTION_CHECKPOINT = "transcript.raw_checkpoint.json"
ALIGNMENT_SUBPROCESS_OUTPUT = "transcript.aligned_subprocess.json"


def transcribe(video_path: str, output_dir: str, cfg) -> dict:
    """
    Transcribe a video file using faster-whisper and, by default, refine the
    word timings with WhisperX forced alignment.

    Returns a dict with:
      - segments: [{id, start, end, text, words}]
      - words:    flattened list of aligned words
      - metadata: cache/version/alignment details
    """
    transcript_path = Path(output_dir) / "transcript.json"
    raw_checkpoint_path = _raw_transcription_checkpoint_path(output_dir)
    alignment_backend = _desired_word_alignment_backend(cfg)

    cached = load_cached_transcript(output_dir)
    if cached is not None:
        if transcript_cache_is_compatible(cached, cfg):
            log.info(f"Loading cached transcript from {transcript_path}")
            return cached
        log.info(
            "Cached transcript uses an older schema or raw word timings; "
            "regenerating transcript with the current alignment pipeline"
        )

    checkpoint = load_cached_raw_transcription_checkpoint(output_dir, video_path, cfg)
    if checkpoint is not None:
        result = checkpoint
        log.info(f"Resuming from raw transcription checkpoint: {raw_checkpoint_path}")
    else:
        result = _run_faster_whisper_transcription(
            video_path,
            cfg,
            alignment_backend,
            raw_checkpoint_path,
        )

    if alignment_backend == "whisperx":
        try:
            result = _align_with_whisperx_resilient(video_path, result, raw_checkpoint_path, output_dir, cfg)
            log.info(f"WhisperX alignment complete: {len(result['words'])} aligned words")
        except Exception as e:
            fallback_for_oom = _is_cuda_out_of_memory(e) and getattr(cfg, "WHISPERX_FALLBACK_TO_RAW_ON_OOM", True)
            fallback_for_crash = (
                "alignment subprocess" in str(e).lower()
                and getattr(cfg, "WHISPERX_FALLBACK_TO_RAW_ON_ALIGNMENT_CRASH", True)
            )
            if fallback_for_oom or fallback_for_crash:
                log.warning(
                    "WhisperX alignment failed after raw transcription was checkpointed; "
                    "falling back to raw faster-whisper word timestamps"
                )
                _clear_torch_cuda_cache()
                result = _fallback_to_raw_word_timestamps(result, reason=str(e))
                log.info(f"Using raw faster-whisper word timestamps: {len(result['words'])} words")
            else:
                raise
    else:
        if not result.get("words"):
            result = _fallback_to_raw_word_timestamps(result, reason=f"WORD_ALIGNMENT_BACKEND={alignment_backend}")
        log.info(f"Using raw faster-whisper word timestamps: {len(result['words'])} words")

    try:
        from word_corrector import apply_corrections_to_transcript
        result = apply_corrections_to_transcript(result, cfg)
    except Exception as e:
        log.warning(f"Word correction skipped: {e}")

    _validate_transcript_word_timings(result)

    _write_json_atomic(transcript_path, result)

    log.info(f"Transcript saved to {transcript_path}")
    return result


def _run_faster_whisper_transcription(
    video_path: str,
    cfg,
    alignment_backend: str,
    raw_checkpoint_path: Path,
) -> dict:
    video_name = Path(video_path).name
    log.info(f"Starting transcription of: {video_path}")
    log.info(f"Model: {cfg.WHISPER_MODEL_SIZE} | Device: {cfg.WHISPER_DEVICE}")

    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise RuntimeError("faster-whisper not installed. Run: pip install faster-whisper") from e

    model = WhisperModel(
        cfg.WHISPER_MODEL_SIZE,
        device=cfg.WHISPER_DEVICE,
        compute_type=cfg.WHISPER_COMPUTE,
    )

    # Keep raw decoder word timestamps even when WhisperX is enabled so we can
    # preserve tokens WhisperX cannot align well (notably number-like tokens).
    segments_iter, info = model.transcribe(
        video_path,
        language=getattr(cfg, "WHISPER_LANGUAGE", "id"),
        word_timestamps=True,
        beam_size=getattr(cfg, "WHISPER_BEAM_SIZE", 5),
        best_of=getattr(cfg, "WHISPER_BEST_OF", 5),
        vad_filter=True,
        vad_parameters={
            "min_silence_duration_ms": 800,
        },
    )

    log.info(f"Detected language: {info.language} (prob: {info.language_probability:.2f})")

    result = {
        "segments": [],
        "words": [],
        "metadata": {
            "schema_version": TRANSCRIPT_SCHEMA_VERSION,
            "transcriber": "faster-whisper",
            "word_alignment_backend": "raw" if alignment_backend != "whisperx" else "raw_checkpoint",
            "desired_word_alignment_backend": alignment_backend,
            "checkpoint_kind": "raw_transcription",
            "source_video_path": str(Path(video_path).resolve()),
            "whisper_model_size": getattr(cfg, "WHISPER_MODEL_SIZE", None),
            "whisper_language": getattr(cfg, "WHISPER_LANGUAGE", None),
            "whisper_beam_size": getattr(cfg, "WHISPER_BEAM_SIZE", None),
            "whisper_best_of": getattr(cfg, "WHISPER_BEST_OF", None),
            "language": info.language,
            "language_probability": round(float(info.language_probability), 6),
            "timestamp_precision": "float_seconds",
            "raw_word_timestamps_available": True,
        },
    }
    total_segments = 0

    for seg in segments_iter:
        seg_data = {
            "id": seg.id,
            "start": _normalize_timestamp(seg.start),
            "end": _normalize_timestamp(seg.end),
            "text": seg.text.strip(),
            "words": [],
            "raw_words": [],
        }

        if seg.words:
            for w in seg.words:
                if w.start is None or w.end is None:
                    continue
                word_text = w.word.strip()
                if not word_text:
                    continue
                word_data = {
                    "word": word_text,
                    "start": _normalize_timestamp(w.start),
                    "end": _normalize_timestamp(w.end),
                    "probability": round(w.probability, 3),
                }
                seg_data["raw_words"].append(word_data)
                if alignment_backend != "whisperx":
                    seg_data["words"].append(word_data)
                    result["words"].append(word_data)

        result["segments"].append(seg_data)
        total_segments += 1

        if total_segments % 50 == 0:
            log.info(f"  {video_name}: transcribed {total_segments} segments... (t={seg.end:.0f}s)")

    result["metadata"]["transcription_complete"] = True
    result["metadata"]["segment_count"] = total_segments
    _write_json_atomic(raw_checkpoint_path, result)
    log.info(f"Raw transcription checkpoint saved to {raw_checkpoint_path}")
    log.info(f"Transcription complete: {total_segments} segments")
    try:
        del model
    except Exception:
        pass
    gc.collect()
    return result


def build_text_chunks(transcript: dict, chunk_duration: float, overlap: float) -> list[dict]:
    """
    Split the full transcript into overlapping time-chunks for LLM processing.
    Each chunk has: start, end, text (full), words (list)
    """
    if not transcript["segments"]:
        return []

    total_end = transcript["segments"][-1]["end"]
    chunks = []
    t = 0.0

    while t < total_end:
        chunk_end = t + chunk_duration
        chunk_segs = [
            s for s in transcript["segments"]
            if s["start"] >= t and s["start"] < chunk_end
        ]
        if not chunk_segs:
            t += chunk_duration - overlap
            continue

        chunk_words = [
            w for w in transcript["words"]
            if w["start"] >= t and w["start"] < chunk_end
        ]

        text_lines = []
        for seg in chunk_segs:
            text_lines.append(f"[{seg['start']:.1f}s] {seg['text']}")

        chunks.append({
            "chunk_start": t,
            "chunk_end": chunk_end,
            "text": "\n".join(text_lines),
            "words": chunk_words,
            "segments": chunk_segs,
        })

        t += chunk_duration - overlap

    log.info(f"Built {len(chunks)} transcript chunks for LLM processing")
    return chunks


def load_cached_transcript(output_dir: str) -> Optional[dict]:
    transcript_path = Path(output_dir) / "transcript.json"
    if not transcript_path.exists():
        return None
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        log.warning(f"Ignoring unreadable transcript cache {transcript_path}: {exc}")
        return None


def load_cached_raw_transcription_checkpoint(output_dir: str, video_path: str, cfg) -> Optional[dict]:
    checkpoint_path = _raw_transcription_checkpoint_path(output_dir)
    if not checkpoint_path.exists():
        return None

    try:
        with open(checkpoint_path, "r", encoding="utf-8") as f:
            checkpoint = json.load(f)
    except Exception as exc:
        log.warning(f"Ignoring unreadable raw transcription checkpoint {checkpoint_path}: {exc}")
        return None

    if not _raw_transcription_checkpoint_is_compatible(checkpoint, video_path, cfg):
        log.info(f"Ignoring stale raw transcription checkpoint: {checkpoint_path}")
        return None

    return checkpoint


def transcript_cache_is_compatible(transcript: dict, cfg) -> bool:
    metadata = transcript.get("metadata", {})
    if metadata.get("schema_version") != TRANSCRIPT_SCHEMA_VERSION:
        return False

    backend = metadata.get("word_alignment_backend")
    desired_backend = _desired_word_alignment_backend(cfg)
    raw_fallback_ok = (
        desired_backend == "whisperx"
        and backend == "raw"
        and bool(metadata.get("whisperx_fallback_reason"))
        and getattr(cfg, "WHISPERX_ACCEPT_RAW_FALLBACK_CACHE", True)
    )
    if backend != desired_backend and not raw_fallback_ok:
        return False

    return _word_timings_are_valid(transcript.get("words", []))


def _desired_word_alignment_backend(cfg) -> str:
    return getattr(cfg, "WORD_ALIGNMENT_BACKEND", "whisperx").lower()


def _raw_transcription_checkpoint_path(output_dir: str) -> Path:
    return Path(output_dir) / RAW_TRANSCRIPTION_CHECKPOINT


def _raw_transcription_checkpoint_is_compatible(checkpoint: dict, video_path: str, cfg) -> bool:
    metadata = checkpoint.get("metadata", {})
    if metadata.get("schema_version") != TRANSCRIPT_SCHEMA_VERSION:
        return False
    if metadata.get("checkpoint_kind") != "raw_transcription":
        return False
    if not metadata.get("transcription_complete"):
        return False

    source_video_path = metadata.get("source_video_path")
    if source_video_path:
        try:
            if str(Path(source_video_path).resolve()).casefold() != str(Path(video_path).resolve()).casefold():
                return False
        except Exception:
            return False

    desired_backend = metadata.get("desired_word_alignment_backend")
    if desired_backend and desired_backend != _desired_word_alignment_backend(cfg):
        return False

    raw_words = _collect_raw_checkpoint_words(checkpoint)
    return bool(raw_words) and _word_timings_are_valid(raw_words)


def _collect_raw_checkpoint_words(checkpoint: dict) -> list:
    words = []
    for seg in checkpoint.get("segments", []) or []:
        for word in seg.get("raw_words", []) or []:
            timed_word = _coerce_timed_word(word)
            if timed_word is not None:
                words.append(timed_word)
    words.sort(key=lambda word: (float(word.get("start", 0.0)), float(word.get("end", 0.0))))
    return words


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(temp_path, path)


def _normalize_timestamp(value) -> float:
    return round(float(value), 6)


def _align_with_whisperx_resilient(
    video_path: str,
    transcript: dict,
    raw_checkpoint_path: Path,
    output_dir: str,
    cfg,
) -> dict:
    if getattr(cfg, "WHISPERX_ALIGN_IN_SUBPROCESS", True):
        return _align_with_whisperx_subprocess(video_path, raw_checkpoint_path, output_dir)
    return _align_with_whisperx(video_path, transcript, cfg)


def _align_with_whisperx_subprocess(video_path: str, raw_checkpoint_path: Path, output_dir: str) -> dict:
    import subprocess

    output_path = Path(output_dir) / ALIGNMENT_SUBPROCESS_OUTPUT
    if output_path.exists():
        try:
            output_path.unlink()
        except OSError:
            pass

    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--align-checkpoint",
        "--video-path",
        video_path,
        "--checkpoint-path",
        str(raw_checkpoint_path),
        "--output-path",
        str(output_path),
    ]
    log.info("Starting WhisperX alignment in isolated subprocess")
    completed = subprocess.run(
        cmd,
        cwd=str(Path(__file__).resolve().parent),
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"WhisperX alignment subprocess failed with exit code {completed.returncode}")
    if not output_path.exists():
        raise RuntimeError(f"WhisperX alignment subprocess did not write {output_path}")

    with open(output_path, "r", encoding="utf-8") as f:
        aligned = json.load(f)

    try:
        output_path.unlink()
    except OSError:
        pass
    return aligned


def _align_with_whisperx(video_path: str, transcript: dict, cfg) -> dict:
    _patch_transformers_wav2vec2_processor_api()
    device = _resolve_whisperx_device(
        getattr(cfg, "WHISPERX_DEVICE", getattr(cfg, "WHISPER_DEVICE", "cuda"))
    )
    language_code = transcript.get("metadata", {}).get("language") or getattr(cfg, "WHISPER_LANGUAGE", "id")
    align_model_name = getattr(cfg, "WHISPERX_ALIGN_MODEL", None) or _default_whisperx_align_model(language_code)
    interpolate_method = getattr(cfg, "WHISPERX_INTERPOLATE_METHOD", "nearest")
    model_dir = getattr(cfg, "WHISPERX_MODEL_DIR", None)
    load_align_model, align, alignment_module = _load_whisperx_alignment_api()

    if not align_model_name:
        raise RuntimeError(
            f"No WhisperX align model configured for language '{language_code}'. "
            "Set WHISPERX_ALIGN_MODEL in config.py to a wav2vec2 alignment model."
        )

    log.info(
        f"Aligning transcript with WhisperX "
        f"(language={language_code}, device={device}, model={align_model_name})"
    )

    max_segment_seconds = float(getattr(cfg, "WHISPERX_MAX_SEGMENT_SECONDS", 30.0) or 0.0)
    alignment_segments = _split_segments_for_whisperx(
        transcript.get("segments", []),
        max_segment_seconds=max_segment_seconds,
    )
    if len(alignment_segments) != len(transcript.get("segments", [])):
        log.info(
            "Split transcript for WhisperX alignment: "
            f"{len(transcript.get('segments', []))} source segments -> "
            f"{len(alignment_segments)} alignment segments "
            f"(max {max_segment_seconds:.0f}s each)"
        )

    # WhisperX's align() helper re-reads DEFAULT_ALIGN_MODELS_HF during
    # preprocessing even when we passed an explicit model name above.
    if hasattr(alignment_module, "DEFAULT_ALIGN_MODELS_HF"):
        alignment_module.DEFAULT_ALIGN_MODELS_HF[language_code] = align_model_name

    model_a = None
    try:
        model_a, align_metadata = load_align_model(
            language_code=language_code,
            device=device,
            model_name=align_model_name,
            model_dir=model_dir,
        )
        aligned = align(
            alignment_segments,
            model_a,
            align_metadata,
            video_path,
            device,
            interpolate_method=interpolate_method,
            return_char_alignments=False,
        )
    finally:
        try:
            del model_a
        except Exception:
            pass
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass

    aligned_result = {
        "segments": [],
        "words": [],
        "metadata": {
            **transcript.get("metadata", {}),
            "schema_version": TRANSCRIPT_SCHEMA_VERSION,
            "word_alignment_backend": "whisperx",
            "whisperx_language": language_code,
            "whisperx_align_model": align_model_name,
        },
    }
    aligned_result["metadata"].pop("checkpoint_kind", None)

    skipped_words = 0
    fallback_words_used = 0
    for idx, seg in enumerate(aligned.get("segments", [])):
        seg_start = _normalize_timestamp(seg["start"])
        seg_end = _normalize_timestamp(seg["end"])
        seg_words = []
        for word in seg.get("words", []) or []:
            word_text = str(word.get("word", "")).strip()
            start = word.get("start")
            end = word.get("end")
            if not word_text:
                continue
            if start is None or end is None:
                skipped_words += 1
                continue

            word_data = {
                "word": word_text,
                "start": _normalize_timestamp(start),
                "end": _normalize_timestamp(end),
            }
            score = word.get("score")
            if score is not None:
                word_data["probability"] = round(float(score), 3)

            seg_words.append(word_data)

        raw_seg_words = _collect_raw_words_in_range(transcript, seg_start, seg_end)

        if not seg_words and raw_seg_words:
            seg_words = [word for word in (_coerce_timed_word(w) for w in raw_seg_words) if word is not None]
            fallback_words_used += len(seg_words)
        else:
            fallback_words = _collect_raw_word_fallbacks(raw_seg_words, seg_words, seg_start, seg_end)
            if fallback_words:
                seg_words = _merge_timed_words(seg_words, fallback_words)
                fallback_words_used += len(fallback_words)

        aligned_result["words"].extend(seg_words)

        aligned_result["segments"].append({
            "id": seg.get("id", idx),
            "start": seg_start,
            "end": seg_end,
            "text": seg.get("text", "").strip(),
            "words": seg_words,
        })

    aligned_result["words"].sort(key=lambda word: (float(word.get("start", 0.0)), float(word.get("end", 0.0))))

    if skipped_words:
        log.warning(f"WhisperX returned {skipped_words} unaligned words without timestamps")
    if fallback_words_used:
        log.info(
            f"Recovered {fallback_words_used} word(s) from raw Whisper timestamps "
            f"when WhisperX could not align them"
        )

    return aligned_result


def _split_segments_for_whisperx(segments: list, max_segment_seconds: float) -> list:
    if max_segment_seconds <= 0:
        return [dict(seg) for seg in segments]

    alignment_segments = []
    for seg in segments:
        seg_start = float(seg.get("start", 0.0))
        seg_end = float(seg.get("end", seg_start))
        raw_words = [word for word in (_coerce_timed_word(w) for w in seg.get("raw_words", []) or []) if word]

        if (seg_end - seg_start) <= max_segment_seconds or not raw_words:
            alignment_segments.append({
                "id": seg.get("id", len(alignment_segments)),
                "start": seg_start,
                "end": seg_end,
                "text": seg.get("text", "").strip(),
                "raw_words": raw_words,
            })
            continue

        group = []
        group_start = None
        for word in raw_words:
            if group and group_start is not None and (word["end"] - group_start) > max_segment_seconds:
                alignment_segments.append(_make_whisperx_alignment_segment(seg, group, len(alignment_segments)))
                group = []
                group_start = None

            if group_start is None:
                group_start = word["start"]
            group.append(word)

        if group:
            alignment_segments.append(_make_whisperx_alignment_segment(seg, group, len(alignment_segments)))

    return alignment_segments


def _make_whisperx_alignment_segment(source_seg: dict, words: list, segment_id: int) -> dict:
    source_start = float(source_seg.get("start", 0.0))
    source_end = float(source_seg.get("end", source_start))
    start = max(source_start, float(words[0]["start"]) - 0.05)
    end = min(source_end, float(words[-1]["end"]) + 0.05)
    if end <= start:
        end = max(start + 0.01, float(words[-1]["end"]))

    return {
        "id": segment_id,
        "start": _normalize_timestamp(start),
        "end": _normalize_timestamp(end),
        "text": " ".join(str(word.get("word", "")).strip() for word in words if str(word.get("word", "")).strip()),
        "raw_words": words,
    }


def _collect_raw_words_in_range(transcript: dict, seg_start: float, seg_end: float, tolerance: float = 0.15) -> list:
    raw_words = []
    for source_seg in transcript.get("segments", []):
        try:
            source_start = float(source_seg.get("start", 0.0))
            source_end = float(source_seg.get("end", source_start))
        except (TypeError, ValueError):
            continue
        if source_end < (seg_start - tolerance) or source_start > (seg_end + tolerance):
            continue
        for word in source_seg.get("raw_words", []) or []:
            timed_word = _coerce_timed_word(word)
            if timed_word is None:
                continue
            if timed_word["start"] >= (seg_start - tolerance) and timed_word["end"] <= (seg_end + tolerance):
                raw_words.append(timed_word)
    return raw_words


def _fallback_to_raw_word_timestamps(transcript: dict, reason: str) -> dict:
    raw_result = {
        "segments": [],
        "words": [],
        "metadata": {
            **transcript.get("metadata", {}),
            "schema_version": TRANSCRIPT_SCHEMA_VERSION,
            "word_alignment_backend": "raw",
            "whisperx_fallback_reason": reason[:500],
        },
    }
    raw_result["metadata"].pop("checkpoint_kind", None)

    for idx, seg in enumerate(transcript.get("segments", [])):
        seg_words = [word for word in (_coerce_timed_word(w) for w in seg.get("raw_words", []) or []) if word]
        raw_result["words"].extend(seg_words)
        raw_result["segments"].append({
            "id": seg.get("id", idx),
            "start": _normalize_timestamp(seg.get("start", 0.0)),
            "end": _normalize_timestamp(seg.get("end", seg.get("start", 0.0))),
            "text": seg.get("text", "").strip(),
            "words": seg_words,
        })

    raw_result["words"].sort(key=lambda word: (float(word.get("start", 0.0)), float(word.get("end", 0.0))))
    return raw_result


def _default_whisperx_align_model(language_code: str) -> Optional[str]:
    default_models_hf = {
        "id": "cahya/wav2vec2-large-xlsr-indonesian",
    }
    return default_models_hf.get(language_code)


def _collect_raw_word_fallbacks(raw_words: list, aligned_words: list, seg_start: float, seg_end: float) -> list:
    aligned_counts = {}
    for word in aligned_words:
        normalized = _normalize_fallback_token(word.get("word", ""))
        if normalized:
            aligned_counts[normalized] = aligned_counts.get(normalized, 0) + 1

    fallback = []
    for word in raw_words or []:
        normalized = _normalize_fallback_token(word.get("word", ""))
        if not normalized:
            continue
        try:
            start = float(word.get("start", 0.0))
            end = float(word.get("end", start))
        except (TypeError, ValueError):
            continue
        if start < (seg_start - 0.15) or end > (seg_end + 0.15):
            continue
        count = aligned_counts.get(normalized, 0)
        if count > 0:
            aligned_counts[normalized] = count - 1
            continue
        if _word_needs_raw_fallback(word.get("word", "")):
            fallback.append(dict(word))

    return fallback


def _merge_timed_words(primary_words: list, fallback_words: list) -> list:
    merged = [dict(word) for word in primary_words]
    for fallback in fallback_words:
        if any(_timed_words_match(existing, fallback) for existing in merged):
            continue
        merged.append(dict(fallback))

    merged.sort(key=lambda word: (float(word.get("start", 0.0)), float(word.get("end", 0.0))))
    return merged


def _timed_words_match(left: dict, right: dict) -> bool:
    left_norm = _normalize_fallback_token(left.get("word", ""))
    right_norm = _normalize_fallback_token(right.get("word", ""))
    if not left_norm or left_norm != right_norm:
        return False

    return (
        abs(float(left.get("start", 0.0)) - float(right.get("start", 0.0))) <= 0.08
        and abs(float(left.get("end", 0.0)) - float(right.get("end", 0.0))) <= 0.08
    )


def _normalize_fallback_token(text: str) -> str:
    return re.sub(r"[^\w]+", "", str(text).lower(), flags=re.UNICODE)


def _word_needs_raw_fallback(text: str) -> bool:
    normalized = _normalize_fallback_token(text)
    return any(ch.isdigit() for ch in normalized)


def _coerce_timed_word(word: dict) -> Optional[dict]:
    word_text = str(word.get("word", "")).strip()
    if not word_text:
        return None

    try:
        start = _normalize_timestamp(word.get("start"))
        end = _normalize_timestamp(word.get("end"))
    except (TypeError, ValueError):
        return None
    if end < start:
        return None

    timed_word = {
        "word": word_text,
        "start": start,
        "end": end,
    }
    probability = word.get("probability", word.get("score"))
    if probability is not None:
        try:
            timed_word["probability"] = round(float(probability), 3)
        except (TypeError, ValueError):
            pass
    return timed_word


def _is_cuda_out_of_memory(exc: Exception) -> bool:
    current = exc
    while current is not None:
        if current.__class__.__name__ == "OutOfMemoryError":
            return True
        if "CUDA out of memory" in str(current):
            return True
        current = current.__cause__ or current.__context__
    return False


def _clear_torch_cuda_cache() -> None:
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def _patch_transformers_wav2vec2_processor_api() -> None:
    """
    WhisperX's alignment helper expects Wav2Vec2Processor.sampling_rate, but
    newer Transformers exposes that on processor.feature_extractor instead.
    """
    try:
        from transformers import Wav2Vec2Processor
    except Exception:
        return

    if hasattr(Wav2Vec2Processor, "sampling_rate"):
        return

    def _sampling_rate(self):
        return getattr(getattr(self, "feature_extractor", None), "sampling_rate", None)

    Wav2Vec2Processor.sampling_rate = property(_sampling_rate)


def _load_whisperx_alignment_api():
    """
    Load only WhisperX's alignment modules directly from site-packages.

    This avoids importing whisperx.__init__, which pulls in pyannote VAD and
    diarization modules that are not needed for karaoke alignment.
    """
    try:
        import whisperx  # noqa: F401
        import whisperx.alignment as alignment_module
        return alignment_module.load_align_model, alignment_module.align, alignment_module
    except Exception:
        pass

    whisperx_dir = _find_whisperx_package_dir()
    package_name = "_proya_whisperx_alignment"
    if package_name not in sys.modules:
        package = types.ModuleType(package_name)
        package.__path__ = [str(whisperx_dir)]
        sys.modules[package_name] = package

        for module_name in ("types", "utils", "audio", "alignment"):
            full_name = f"{package_name}.{module_name}"
            if full_name in sys.modules:
                continue

            spec = importlib.util.spec_from_file_location(
                full_name,
                whisperx_dir / f"{module_name}.py",
            )
            if spec is None or spec.loader is None:
                raise RuntimeError(f"Failed to load WhisperX module: {module_name}")

            module = importlib.util.module_from_spec(spec)
            sys.modules[full_name] = module
            spec.loader.exec_module(module)

    alignment_module = sys.modules[f"{package_name}.alignment"]
    return alignment_module.load_align_model, alignment_module.align, alignment_module


def _find_whisperx_package_dir() -> Path:
    for path_entry in sys.path:
        if not path_entry:
            continue
        candidate = Path(path_entry) / "whisperx"
        if (candidate / "alignment.py").exists():
            return candidate

    raise RuntimeError(
        "whisperx is required for precise karaoke timing. "
        "Install it with: pip install whisperx"
    )


def _resolve_whisperx_device(preferred_device: str) -> str:
    preferred = str(preferred_device).lower()
    if preferred != "cuda":
        return preferred

    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass

    log.warning("WHISPERX_DEVICE is set to cuda, but torch CUDA is unavailable; falling back to CPU alignment")
    return "cpu"


def _word_timings_are_valid(words: list) -> bool:
    try:
        _validate_word_timings(words)
        return True
    except RuntimeError:
        return False


def _validate_transcript_word_timings(transcript: dict) -> None:
    _validate_word_timings(transcript.get("words", []))


def _validate_word_timings(words: list) -> None:
    prev_start = -1.0
    for idx, word in enumerate(words):
        start = word.get("start")
        end = word.get("end")
        if start is None or end is None:
            raise RuntimeError(f"Transcript word {idx} is missing start/end timestamps")

        start_f = float(start)
        end_f = float(end)

        if end_f < start_f:
            raise RuntimeError(f"Transcript word {idx} has end < start ({start_f} > {end_f})")
        if start_f + 1e-6 < prev_start:
            raise RuntimeError(f"Transcript word {idx} starts before the previous word ({start_f} < {prev_start})")

        prev_start = start_f


def _run_align_checkpoint_cli(video_path: str, checkpoint_path: str, output_path: str) -> None:
    import config as cfg

    with open(checkpoint_path, "r", encoding="utf-8") as f:
        transcript = json.load(f)

    aligned = _align_with_whisperx(video_path, transcript, cfg)
    _validate_transcript_word_timings(aligned)
    _write_json_atomic(Path(output_path), aligned)
    log.info(f"WhisperX aligned transcript saved to {output_path}")


def _main() -> int:
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="Transcriber helper commands")
    parser.add_argument("--align-checkpoint", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--video-path", help=argparse.SUPPRESS)
    parser.add_argument("--checkpoint-path", help=argparse.SUPPRESS)
    parser.add_argument("--output-path", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.align_checkpoint:
        if not args.video_path or not args.checkpoint_path or not args.output_path:
            parser.error("--video-path, --checkpoint-path, and --output-path are required")
        try:
            _run_align_checkpoint_cli(args.video_path, args.checkpoint_path, args.output_path)
        except Exception:
            log.exception("WhisperX alignment helper failed")
            return 1
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
