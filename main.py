#!/usr/bin/env python3
# =============================================================================
#  main.py — PROYA Livestream Clip Automation Pipeline
#  
#  REQUIREMENTS (install before first run):
#    pip install faster-whisper ultralytics moviepy opencv-python openai
#    pip install pillow streamlit tqdm
#
#  LM STUDIO SETUP:
#    1. Download LM Studio: https://lmstudio.ai
#    2. Download a model (recommended: Gemma 3 12B Instruct Q4)
#    3. Go to "Local Server" tab → Start Server
#    4. Make sure the model is loaded (green indicator)
#
#  USAGE:
#    # Full pipeline (transcribe → detect moments → scan video → cut → edit):
#    python main.py --video livestream.mp4
#
#    # Skip to a specific stage (if previous stages are cached):
#    python main.py --video livestream.mp4 --skip-transcribe
#    python main.py --video livestream.mp4 --skip-transcribe --skip-moments
#
#    # Only cut clips, skip editing (faster for testing):
#    python main.py --video livestream.mp4 --cut-only
#
#    # Train YOLO on your product dataset (one-time):
#    python main.py --train-yolo
#
#    # Launch Streamlit web UI:
#    streamlit run app.py
# =============================================================================

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from bisect import bisect_left, bisect_right
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from stage_cache import write_stage_fingerprint

# ── Configure logging ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("pipeline.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("proya.main")


def _build_clip_job(moment: dict, index: int, output_dir: str, raw_dir: Path) -> dict:
    clip_id = moment.get("clip_id", f"clip_{index+1:04d}")
    start = moment["start"]
    end = moment["end"]
    score = moment["score"]
    product = moment.get("product", "general")
    clip_type = moment.get("clip_type", "general")
    safe_hook = _safe_filename(moment.get("hook", clip_id))[:40]
    output_filename = f"{clip_id}_score{int(score)}_{safe_hook}.mp4"
    version_dir = _clip_version_dir(moment, clip_id)
    output_relative_path = f"{version_dir}/{output_filename}" if version_dir else output_filename
    return {
        "index": index,
        "clip_id": clip_id,
        "start": start,
        "end": end,
        "score": score,
        "product": product,
        "clip_type": clip_type,
        "moment": moment,
        "version_dir": version_dir,
        "output_filename": output_filename,
        "output_relative_path": output_relative_path,
        "output_path": str(Path(output_dir) / output_relative_path),
        "raw_path": str(raw_dir / f"{clip_id}_raw.mp4"),
    }


def _clip_version_dir(moment: dict, clip_id: str) -> str | None:
    variant = moment.get("_variant")

    variant_index = getattr(variant, "variant_index", None) if variant is not None else None
    if variant_index is not None:
        try:
            return f"v{int(variant_index)}"
        except (TypeError, ValueError):
            pass

    variant_id = str(getattr(variant, "variant_id", "") or "")
    match = re.match(r"^(v\d+)(?:_|$)", variant_id)
    if match:
        return match.group(1)

    match = re.match(r"^clip_\d+_(v\d+)(?:_|$)", clip_id)
    if match:
        return match.group(1)

    return None


def _process_clip_job(job: dict, video_path: str, transcript_words: list, product_events: list, cut_only: bool, cfg) -> dict:
    from ffmpeg_editor import cut_raw_clip, edit_clip, get_words_for_clip
    from vision_scanner import get_events_for_clip

    output_path = job["output_path"]
    raw_path    = job["raw_path"]
    os.makedirs(Path(output_path).parent, exist_ok=True)

    if Path(output_path).exists():
        return {
            "clip_id": job["clip_id"],
            "status": "skipped",
            "output_filename": job["output_filename"],
            "manifest": _build_manifest_row(
                job,
                len(job.get("clip_product_events") or []),
                "skipped",
            ),
        }

    # Variant-aware cut — applies mirror/speed/grade/crop at cut time via FFmpeg
    variant = job["moment"].get("_variant", None)
    variant_baked = False
    should_bake_variant = variant is not None and bool(getattr(cfg, "VARIANT_FFMPEG_BAKE", True))
    if should_bake_variant:
        try:
            from variation_engine import cut_raw_clip_with_variant
            cut_ok = cut_raw_clip_with_variant(
                video_path, job["start"], job["end"], raw_path, variant, cfg
            )
            variant_baked = True
        except ImportError:
            cut_ok = cut_raw_clip(video_path, job["start"], job["end"], raw_path, cfg=cfg)
    else:
        cut_ok = cut_raw_clip(video_path, job["start"], job["end"], raw_path, cfg=cfg)

    if not cut_ok:
        return {
            "clip_id": job["clip_id"],
            "status": "failed",
            "output_filename": job["output_filename"],
            "manifest": _build_manifest_row(job, 0, "failed"),
        }

    if cut_only:
        shutil.copy2(raw_path, output_path)
        if Path(raw_path).exists():
            os.remove(raw_path)
        return {
            "clip_id": job["clip_id"],
            "status": "ok",
            "output_filename": job["output_filename"],
            "manifest": _build_manifest_row(job, 0, "ok"),
        }

    # Apply variant style overrides (font/color/zoom/y-pos) to cfg
    if variant is not None:
        try:
            from variation_engine import apply_variant_to_cfg
            edit_cfg = apply_variant_to_cfg(cfg, variant)
            setattr(edit_cfg, "_variant_transforms_baked", variant_baked)
        except ImportError:
            edit_cfg = cfg
    else:
        edit_cfg = cfg

    clip_words = job.get("clip_words")
    if clip_words is None:
        clip_words = get_words_for_clip(transcript_words, job["start"], job["end"])
    clip_product_events = job.get("clip_product_events")
    if clip_product_events is None:
        clip_product_events = get_events_for_clip(product_events, job["start"], job["end"])

    mirror = bool(getattr(variant, "mirror", False)) if variant is not None else False
    crop_x_offset = float(getattr(variant, "crop_x_offset", 0.0)) if variant is not None else 0.0
    if mirror or abs(crop_x_offset) > 0.005:
        clip_product_events = _remap_events_for_spatial_variant(
            clip_product_events,
            mirror=mirror,
            crop_x_offset=crop_x_offset,
        )

    speed_ramp = getattr(variant, "speed_ramp", 1.0) if variant is not None else 1.0
    if abs(speed_ramp - 1.0) > 0.02:
        clip_words = _remap_words_for_speed_ramp(clip_words, speed_ramp)
        clip_product_events = _remap_events_for_speed_ramp(clip_product_events, speed_ramp)

    edit_ok = edit_clip(
        raw_clip_path=raw_path,
        output_path=output_path,
        moment=job["moment"],
        clip_words=clip_words,
        product_events=clip_product_events,
        cfg=edit_cfg,
    )

    if Path(raw_path).exists():
        os.remove(raw_path)

    return {
        "clip_id": job["clip_id"],
        "status": "ok" if edit_ok else "failed",
        "output_filename": job["output_filename"],
        "manifest": _build_manifest_row(job, len(clip_product_events), "ok" if edit_ok else "failed"),
    }


def _build_manifest_row(job: dict, product_event_count: int, status: str) -> dict:
    moment = job["moment"]
    return {
        "clip_id": job["clip_id"],
        "version_dir": job.get("version_dir") or "",
        "output_file": job.get("output_relative_path") or job["output_filename"],
        "start": job["start"],
        "end": job["end"],
        "duration": round(job["end"] - job["start"], 1),
        "score": job["score"],
        "hook": moment.get("hook", ""),
        "product": job["product"],
        "clip_type": job["clip_type"],
        "reason": moment.get("reason", ""),
        "product_events": product_event_count,
        "status": status,
    }


def _build_clip_word_index(words: list) -> dict:
    ordered = sorted(words or [], key=lambda word: (float(word.get("start", 0.0)), float(word.get("end", 0.0))))
    return {
        "words": ordered,
        "starts": [float(word.get("start", 0.0)) for word in ordered],
    }


def _get_words_for_clip_indexed(index: dict, clip_start: float, clip_end: float) -> list:
    words = index.get("words", [])
    starts = index.get("starts", [])
    left = bisect_left(starts, clip_start)
    right = bisect_right(starts, clip_end + 0.5)
    return [
        {
            "word": w["word"],
            "start": round(float(w["start"]) - clip_start, 6),
            "end": round(float(w["end"]) - clip_start, 6),
        }
        for w in words[left:right]
        if float(w.get("end", 0.0)) <= clip_end + 0.5
    ]


def _build_clip_event_index(events: list) -> dict:
    ordered = sorted(events or [], key=lambda event: float(event.get("start_time", 0.0)))
    return {
        "events": ordered,
        "starts": [float(event.get("start_time", 0.0)) for event in ordered],
    }


def _get_events_for_clip_indexed(index: dict, clip_start: float, clip_end: float) -> list:
    events = index.get("events", [])
    starts = index.get("starts", [])
    right = bisect_right(starts, clip_end)
    clip_events = []
    for event in events[:right]:
        event_start = float(event.get("start_time", 0.0))
        event_end = float(event.get("end_time", event_start))
        if event_end < clip_start or event_start > clip_end:
            continue
        clip_events.append({
            **event,
            "relative_start": round(event_start - clip_start, 3),
            "relative_end": round(event_end - clip_start, 3),
            "relative_track": [
                {
                    **sample,
                    "relative_time": round(float(sample.get("time", clip_start)) - clip_start, 3),
                }
                for sample in event.get("track", [])
                if clip_start <= float(sample.get("time", clip_start - 1)) <= clip_end
            ],
        })
    return clip_events


def _attach_precomputed_clip_contexts(jobs: list, transcript_words: list, product_events: list) -> None:
    word_index = _build_clip_word_index(transcript_words)
    event_index = _build_clip_event_index(product_events)
    context_cache = {}

    for job in jobs:
        key = (float(job["start"]), float(job["end"]))
        context = context_cache.get(key)
        if context is None:
            context = {
                "clip_words": _get_words_for_clip_indexed(word_index, key[0], key[1]),
                "clip_product_events": _get_events_for_clip_indexed(event_index, key[0], key[1]),
            }
            context_cache[key] = context
        job["clip_words"] = context["clip_words"]
        job["clip_product_events"] = context["clip_product_events"]


def _score_rendered_clips(jobs: list, manifest: list, output_dir: str, cfg, progress_callback=None) -> list:
    if not getattr(cfg, "SCORER_ENABLED", True):
        return []

    try:
        from clip_scorer import score_clip_variants, write_score_artifacts
    except Exception as exc:
        log.warning(f"Clip scorer unavailable; skipping post-render scoring: {exc}")
        return []

    manifest_by_clip = {
        row.get("clip_id"): row
        for row in manifest
        if isinstance(row, dict) and row.get("clip_id")
    }
    score_jobs = []
    for job in jobs:
        row = manifest_by_clip.get(job.get("clip_id"))
        if not row or row.get("status") == "failed":
            continue
        output_path = Path(job["output_path"])
        if output_path.exists():
            score_jobs.append((job, row, output_path))

    if not score_jobs:
        return []

    total = len(score_jobs)
    log.info(f"\n-- STAGE 5: CLIP SCORING ------------------------------------------------")
    log.info(f"  Grouped scoring for {total} rendered clip variant(s)")
    _report(progress_callback, "scoring", 95, f"Scoring {total} rendered clips...")

    entries = []
    for job, row, output_path in score_jobs:
        transcript_input = job.get("clip_words") or job["moment"].get("selected_text", "")
        entries.append(
            {
                "clip_path": output_path,
                "transcript": transcript_input,
                "product": job.get("product", "general"),
                "clip_id": job.get("clip_id"),
                "output_file": row.get("output_file"),
                "version_dir": row.get("version_dir", ""),
                "hook": row.get("hook", ""),
                "clip_type": row.get("clip_type", ""),
                "source_moment_score": row.get("score"),
            }
        )

    scores, groups, stats = score_clip_variants(entries, cfg=cfg)
    scores_by_clip_id = {
        score.get("clip_id"): score
        for score in scores
        if isinstance(score, dict) and score.get("clip_id")
    }

    for index, (job, row, _output_path) in enumerate(score_jobs, start=1):
        score = scores_by_clip_id.get(job.get("clip_id"))
        if not score:
            log.warning(f"    Score failed for {job['clip_id']}: missing grouped score")
            continue
        _attach_score_to_manifest(row, score, cfg)

        if index % 5 == 0 or index == total:
            pct = 95 + int((index / total) * 4)
            log.info(f"    Scoring progress: {index}/{total} done")
            _report(
                progress_callback,
                "scoring",
                min(99, pct),
                f"Scored {index}/{total} rendered clips...",
                event="clip_scoring_progress",
                clips_scored=index,
                clips_total=total,
            )

    if scores:
        artifacts = write_score_artifacts(scores, output_dir, groups=groups, optimization_stats=stats, cfg=cfg)
        log.info(
            "  Grouped scoring saved %s full scoring call(s): previous=%s actual=%s",
            stats.get("saved_scoring_calls", 0),
            stats.get("previous_scoring_calls", total),
            stats.get("actual_scoring_calls", len(groups)),
        )
        log.info(
            "  Score cache: %s cached, %s fresh",
            stats.get("cached_score_count", 0),
            stats.get("fresh_score_count", len(scores)),
        )
        log.info(
            "  Merged Qwen text calls saved: %s (previous=%s actual=%s)",
            stats.get("saved_text_qwen_calls", 0),
            stats.get("previous_text_qwen_calls", 0),
            stats.get("actual_text_qwen_calls", 0),
        )
        log.info(f"  Scores summary: {artifacts.get('summary_path')}")
        if artifacts.get("scores_report_path"):
            log.info(f"  Scores report:  {artifacts.get('scores_report_path')}")

    return scores


def _score_rendered_clip_host_focus(
    scores: list,
    manifest: list,
    output_dir: str,
    cfg,
    progress_callback=None,
) -> list:
    if not scores or not bool(getattr(cfg, "SCORER_VISION_ENABLED", False)):
        log.info("Skipping Qwen-VL host-focus scoring (disabled or no text scores)")
        return scores

    log.info("\n-- STAGE 6: HOST FOCUS VISION SCORING (Qwen-VL) --------------------------")
    _report(progress_callback, "scoring", 99, "Scoring host focus with Qwen-VL...")
    vision_ready = _start_vision_model_stage(cfg)
    if not vision_ready and _model_management_enabled(cfg):
        log.warning("Skipping host-focus scoring because Qwen-VL did not become ready")
        _finish_vision_model_stage(cfg)
        return scores

    try:
        from clip_scorer import apply_host_focus_vision_scores, write_score_artifacts
    except Exception as exc:
        log.warning(f"Clip scorer vision stage unavailable: {exc}")
        return scores

    try:
        updated_scores, updated_groups, vision_stats = apply_host_focus_vision_scores(
            scores,
            cfg=cfg,
            print_progress=False,
        )
        scores_by_clip_id = {
            score.get("clip_id"): score
            for score in updated_scores
            if isinstance(score, dict) and score.get("clip_id")
        }
        for row in manifest:
            if not isinstance(row, dict):
                continue
            score = scores_by_clip_id.get(row.get("clip_id"))
            if score:
                _attach_score_to_manifest(row, score, cfg)

        artifacts = write_score_artifacts(
            updated_scores,
            output_dir,
            groups=updated_groups,
            optimization_stats={"vision_scoring": vision_stats},
            cfg=cfg,
        )
        log.info(
            "  Host-focus vision scored %s/%s base group(s)",
            vision_stats.get("vision_scored_groups", 0),
            vision_stats.get("vision_base_group_count", 0),
        )
        log.info(f"  Updated scores summary: {artifacts.get('summary_path')}")
        return updated_scores
    finally:
        _finish_vision_model_stage(cfg)


def _attach_score_to_manifest(row: dict, score: dict, cfg) -> None:
    row["scorer_base_clip_id"] = score.get("base_clip_id")
    row["scorer_variant_id"] = score.get("variant_id")
    row["scorer_total_score"] = score.get("total_score")
    row["scorer_content_score"] = score.get("content_score")
    row["scorer_visual_score"] = score.get("visual_score")
    row["scorer_host_focus_score"] = score.get("host_focus_score")
    row["scorer_quality_score"] = score.get("quality_score")
    row["scorer_engagement_score"] = score.get("engagement_score")
    row["scorer_similarity_score"] = score.get("similarity_score")
    row["scorer_flags"] = score.get("flags", [])
    row["scorer_similarity_flags"] = score.get("similarity_flags", [])
    row["scorer_summary"] = score.get("summary", "")
    row["scorer_exported"] = bool(score.get("exported", True))
    row["scorer_inherited_base_scores"] = bool(score.get("inherited_base_scores", False))

    threshold = float(getattr(cfg, "SCORER_MIN_SCORE_TO_EXPORT", 0.0) or 0.0)
    if threshold > 0.0 and not row["scorer_exported"] and row.get("status") in {"ok", "skipped"}:
        row["status"] = "filtered_low_score"
    if score.get("status") == "filtered_low_variant" and row.get("status") in {"ok", "skipped"}:
        row["status"] = "filtered_low_variant"


def _remap_words_for_speed_ramp(words: list, speed_ramp: float) -> list:
    """Map clip-relative word timestamps onto a speed-ramped output timeline."""
    if abs(speed_ramp - 1.0) <= 0.02:
        return words

    remapped = []
    for word in words:
        mapped = dict(word)
        mapped["start"] = round(float(word["start"]) / speed_ramp, 6)
        mapped["end"] = round(float(word["end"]) / speed_ramp, 6)
        remapped.append(mapped)
    return remapped


def _remap_events_for_spatial_variant(events: list, mirror: bool, crop_x_offset: float) -> list:
    """Map product bbox coordinates into the rendered clip's spatial coordinate system."""
    if not mirror and abs(crop_x_offset) <= 0.005:
        return events

    remapped = []
    for event in events:
        mapped = dict(event)
        frame_w = float(event.get("frame_w") or 0)
        frame_h = float(event.get("frame_h") or 0)

        def remap_bbox(bbox):
            return _remap_bbox_for_variant(bbox, frame_w, frame_h, mirror, crop_x_offset)

        for key in ("best_bbox", "start_bbox", "end_bbox"):
            if event.get(key):
                mapped[key] = remap_bbox(event.get(key))

        if event.get("relative_track"):
            mapped["relative_track"] = [
                {
                    **sample,
                    "bbox": remap_bbox(sample.get("bbox")),
                }
                for sample in event["relative_track"]
            ]

        remapped.append(mapped)
    return remapped


def _remap_bbox_for_variant(bbox, frame_w: float, frame_h: float, mirror: bool, crop_x_offset: float):
    if not bbox or frame_w <= 0 or frame_h <= 0:
        return bbox

    x1, y1, x2, y2 = [float(v) for v in bbox]
    out_w = frame_w
    out_h = frame_h

    if abs(crop_x_offset) > 0.005:
        crop_w = frame_w * (1.0 - abs(crop_x_offset))
        crop_x = frame_w * crop_x_offset if crop_x_offset > 0 else 0.0
        if crop_w > 1.0:
            scale_x = frame_w / crop_w
            x1 = (x1 - crop_x) * scale_x
            x2 = (x2 - crop_x) * scale_x
            x1 = max(0.0, min(out_w, x1))
            x2 = max(0.0, min(out_w, x2))

    if mirror:
        x1, x2 = out_w - x2, out_w - x1

    y1 = max(0.0, min(out_h, y1))
    y2 = max(0.0, min(out_h, y2))
    x1 = max(0.0, min(out_w, x1))
    x2 = max(0.0, min(out_w, x2))
    if x2 < x1:
        x1, x2 = x2, x1
    if y2 < y1:
        y1, y2 = y2, y1

    return [round(x1, 3), round(y1, 3), round(x2, 3), round(y2, 3)]


def _remap_events_for_speed_ramp(events: list, speed_ramp: float) -> list:
    """Map clip-relative product event timestamps onto a speed-ramped output timeline."""
    if abs(speed_ramp - 1.0) <= 0.02:
        return events

    remapped = []
    for event in events:
        mapped = dict(event)
        rel_start = event.get("relative_start")
        rel_end = event.get("relative_end")
        if rel_start is not None:
            mapped["relative_start"] = round(float(rel_start) / speed_ramp, 6)
        if rel_end is not None:
            mapped["relative_end"] = round(float(rel_end) / speed_ramp, 6)
        if rel_start is not None and rel_end is not None:
            mapped["duration"] = round((float(rel_end) - float(rel_start)) / speed_ramp, 6)
        if event.get("relative_track"):
            mapped["relative_track"] = [
                {
                    **sample,
                    "relative_time": round(float(sample["relative_time"]) / speed_ramp, 6),
                }
                for sample in event["relative_track"]
                if sample.get("relative_time") is not None
            ]
        remapped.append(mapped)
    return remapped


def run_pipeline(
    video_path: str,
    skip_transcribe: bool = False,
    skip_moments: bool = False,
    skip_vision: bool = False,
    cut_only: bool = False,
    max_clips: int = None,
    min_score: float = None,
    force_rescore: bool = False,
    output_tag: str | None = None,
    working_tag: str | None = None,
    progress_callback=None,   # optional: fn(stage, pct, message, **payload)
):
    """
    Full pipeline orchestrator. All stages cache their results so you can
    safely re-run after a crash — it picks up where it left off.
    """
    import config as cfg

    # Allow runtime overrides
    if min_score is not None:
        cfg.MIN_SCORE = min_score
    if force_rescore:
        cfg.SCORER_FORCE_RESCORE = True
    _sync_lm_studio_model_ids(cfg)

    # ── Validate inputs ───────────────────────────────────────────────────────
    if not Path(video_path).exists():
        raise FileNotFoundError(f"Input video not found: {video_path}")

    base_stem = Path(video_path).stem
    working_stem = base_stem
    if working_tag:
        safe_working_tag = _safe_filename(working_tag)
        working_stem = f"{base_stem}__{safe_working_tag}"
    working_dir = str(Path(cfg.WORKING_DIR) / working_stem)

    output_stem = base_stem
    if output_tag:
        safe_tag = _safe_filename(output_tag)
        output_stem = f"{output_stem}__{safe_tag}"
    output_dir = str(Path(cfg.OUTPUT_DIR) / output_stem)
    os.makedirs(working_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    _report(progress_callback, "init", 0, f"Pipeline started for: {video_path}")
    log.info("=" * 70)
    log.info("PROYA LIVESTREAM CLIP PIPELINE")
    log.info(f"  Input:      {video_path}")
    log.info(f"  Working:    {working_dir}")
    log.info(f"  Output:     {output_dir}")
    if working_tag:
        log.info(f"  Working tag:{working_tag}")
    if output_tag:
        log.info(f"  Rerun tag:   {output_tag}")
    log.info(f"  LM Studio:  {cfg.LM_STUDIO_BASE_URL}")
    log.info("=" * 70)

    _validate_startup(video_path, output_dir, cfg, skip_moments=skip_moments, skip_vision=skip_vision)
    _enforce_text_model_priority_at_pipeline_start(cfg)

    pipeline_start = time.time()
    text_model_stage_started = False

    # ══════════════════════════════════════════════════════════════════════════
    # STAGE 1: TRANSCRIPTION
    # ══════════════════════════════════════════════════════════════════════════
    if not skip_transcribe:
        _report(progress_callback, "transcribe", 5, "Transcribing audio (Whisper)...")
        log.info("\n── STAGE 1: TRANSCRIPTION ─────────────────────────────────────────")

        from transcriber import transcribe, build_text_chunks
        t0 = time.time()
        transcript = transcribe(video_path, working_dir, cfg)
        log.info(f"Transcription done in {_fmt_time(time.time()-t0)}")
    else:
        log.info("Skipping transcription (using cached)")
        from transcriber import (
            build_text_chunks,
            load_cached_transcript,
            transcript_cache_is_compatible,
            transcribe,
        )
        transcript_path = Path(working_dir) / "transcript.json"
        transcript = load_cached_transcript(working_dir)
        if transcript is None:
            raise FileNotFoundError(f"No cached transcript at {transcript_path}. Run without --skip-transcribe first.")
        if not transcript_cache_is_compatible(transcript, cfg):
            log.info("Cached transcript is outdated or uses raw word timings; rebuilding aligned transcript")
            transcript = transcribe(video_path, working_dir, cfg)

    write_stage_fingerprint(Path(working_dir) / "transcript.json", video_path, cfg, "transcribe")
    _report(progress_callback, "transcribe", 20, f"Transcript: {len(transcript['words'])} words")

    if (not skip_moments) or bool(getattr(cfg, "SCORER_ENABLED", True)):
        text_model_stage_started = _start_text_model_stage(cfg)

    # ══════════════════════════════════════════════════════════════════════════
    # STAGE 2: LLM MOMENT DETECTION (LM Studio)
    # ══════════════════════════════════════════════════════════════════════════
    if not skip_moments:
        _report(progress_callback, "moments", 22, "Detecting moments with LLM (LM Studio)...")
        log.info("\n── STAGE 2: MOMENT DETECTION (LM Studio) ─────────────────────────")

        from transcriber import build_text_chunks
        from moment_detector import detect_moments

        chunks = build_text_chunks(transcript, cfg.CHUNK_DURATION, cfg.CHUNK_OVERLAP)
        t0 = time.time()
        moments = detect_moments(chunks, working_dir, cfg)
        log.info(f"Moment detection done in {_fmt_time(time.time()-t0)}")
    else:
        log.info("Skipping moment detection (using cached)")
        import json
        moments_path = Path(working_dir) / "moments.json"
        if not moments_path.exists():
            raise FileNotFoundError(f"No cached moments at {moments_path}. Run without --skip-moments first.")
        with open(moments_path, "r", encoding="utf-8") as f:
            moments = json.load(f)

    if not moments:
        log.warning("No moments detected! Check your LM Studio connection and transcript quality.")
        if text_model_stage_started:
            _finish_text_model_stage(cfg)
        return {"clips_created": 0, "clips_failed": 0, "moments_found": 0}

    write_stage_fingerprint(Path(working_dir) / "moments.json", video_path, cfg, "llm")

    # Apply max_clips limit (takes highest scored first since list is sorted)
    if max_clips and len(moments) > max_clips:
        log.info(f"Limiting to top {max_clips} clips (from {len(moments)} total)")
        moments = moments[:max_clips]

    log.info(f"Moments to process: {len(moments)}")
    _report(progress_callback, "moments", 35, f"Found {len(moments)} clip moments")

    # ── Variation expansion ───────────────────────────────────────────────────
    n_variants = getattr(cfg, "VARIANTS_PER_CLIP", 1)
    if n_variants > 1:
        try:
            from variation_engine import expand_moments_with_variants
            variant_seed = getattr(cfg, "VARIANT_SEED", 42)
            log.info(f"\n── VARIATION ENGINE ──────────────────────────────────────────────")
            log.info(f"  Base moments: {len(moments)} | Variants per clip: {n_variants}")
            moments = expand_moments_with_variants(moments, cfg, n_variants=n_variants, seed=variant_seed)
            log.info(f"  Total clip jobs after expansion: {len(moments)}")
        except ImportError:
            log.warning("variation_engine.py not found — skipping variations")

    # ══════════════════════════════════════════════════════════════════════════
    # STAGE 3: VISION SCAN (YOLO)
    # ══════════════════════════════════════════════════════════════════════════
    product_events = []
    vision_scan_ranges = []

    if not skip_vision:
        yolo_available = Path(cfg.YOLO_WEIGHTS).exists()
        if not yolo_available:
            log.warning(f"YOLO weights not found at {cfg.YOLO_WEIGHTS}. Skipping vision scan.")
            log.warning("Run 'python main.py --train-yolo' to train your product detector first.")
        else:
            _report(progress_callback, "vision", 37, "Scanning for products (YOLO)...")
            log.info("\n── STAGE 3: VISION SCAN (YOLOv8) ────────────────────────────────")

            from vision_scanner import build_scan_ranges_from_moments, scan_video_for_products
            t0 = time.time()
            scan_ranges = build_scan_ranges_from_moments(moments, cfg)
            vision_scan_ranges = scan_ranges
            product_events = scan_video_for_products(
                video_path,
                working_dir,
                cfg,
                scan_ranges=scan_ranges,
            )
            log.info(f"Vision scan done in {_fmt_time(time.time()-t0)}")
            log.info(f"Product events found: {len(product_events)}")
    else:
        log.info("Skipping vision scan (using cached or disabled)")
        import json
        detections_path = Path(working_dir) / "product_detections.json"
        if detections_path.exists():
            with open(detections_path, "r") as f:
                cached_product_events = json.load(f)
            try:
                from vision_scanner import _is_valid_cached_events
                if _is_valid_cached_events(cached_product_events):
                    product_events = cached_product_events
                    log.info(f"Loaded {len(product_events)} cached product events")
                else:
                    log.warning("Cached product events are outdated; rerun without --skip-vision to rebuild them")
                    product_events = []
            except Exception:
                product_events = cached_product_events
                log.info(f"Loaded {len(product_events)} cached product events")

    detections_path = Path(working_dir) / "product_detections.json"
    if detections_path.exists():
        if not vision_scan_ranges:
            try:
                from vision_scanner import build_scan_ranges_from_moments
                vision_scan_ranges = build_scan_ranges_from_moments(moments, cfg)
            except Exception:
                vision_scan_ranges = []
        write_stage_fingerprint(
            detections_path,
            video_path,
            cfg,
            "yolo",
            extra={"scan_ranges": vision_scan_ranges},
        )

    _report(progress_callback, "vision", 50, f"{len(product_events)} product events loaded")

    # ══════════════════════════════════════════════════════════════════════════
    # STAGE 4: CUT + EDIT CLIPS
    # ══════════════════════════════════════════════════════════════════════════
    log.info("\n── STAGE 4: CUT & EDIT CLIPS ─────────────────────────────────────")

    from vision_scanner import get_events_for_clip

    raw_dir = Path(working_dir) / "raw_cuts"
    raw_dir.mkdir(exist_ok=True)

    clips_created = 0
    clips_failed  = 0
    clips_skipped = 0
    manifest      = []
    manifest_path = Path(output_dir) / "manifest.json"

    jobs = [_build_clip_job(moment, i, output_dir, raw_dir) for i, moment in enumerate(moments)]
    _attach_precomputed_clip_contexts(jobs, transcript["words"], product_events)
    max_workers = max(1, int(getattr(cfg, "MAX_PARALLEL_CLIPS", 6)))
    edit_log_every = max(1, int(getattr(cfg, "EDIT_LOG_EVERY_N", 25)))
    log.info(f"  Total jobs: {len(jobs)} | Parallel workers: {max_workers}")

    if getattr(cfg, "EDIT_LOG_CLIP_PLAN", False):
        for job in jobs:
            log.info(
                f"  [{job['index']+1:03d}/{len(jobs):03d}] {job['clip_id']} | "
                f"t={job['start']:.1f}s-{job['end']:.1f}s | score={job['score']} | "
                f"type={job['clip_type']} | product={job['product']}"
            )

    completed = 0
    _report(
        progress_callback,
        "editing",
        50,
        f"Rendering {len(jobs)} clips...",
        event="clip_batch_start",
        clips_total=len(jobs),
        clips_completed=0,
        clips_created=0,
        clips_failed=0,
        clips_skipped=0,
    )
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(
                _process_clip_job,
                job,
                video_path,
                transcript["words"],
                product_events,
                cut_only,
                cfg,
            ): job
            for job in jobs
        }

        for future in as_completed(future_map):
            job = future_map[future]
            completed += 1
            pct = 50 + int((completed / len(jobs)) * 45)
            clip_status = "failed"

            try:
                result = future.result()
            except Exception as e:
                clips_failed += 1
                log.error(f"    Worker failed for {job['clip_id']}: {e}")
                manifest.append(_build_manifest_row(job, 0, "failed"))
                _write_json_atomic(manifest_path, manifest)
            else:
                clip_status = result["status"]
                if result["status"] == "skipped":
                    clips_skipped += 1
                    clips_created += 1
                    log.debug(f"    Already exists, skipping: {result['output_filename']}")
                elif result["status"] == "ok":
                    clips_created += 1
                    if getattr(cfg, "EDIT_LOG_CREATED_CLIPS", False):
                        log.info(f"    Created: {result['output_filename']}")
                else:
                    clips_failed += 1
                    log.error(f"    Edit failed for {job['clip_id']}")

                if result["manifest"]:
                    manifest.append(result["manifest"])
                    _write_json_atomic(manifest_path, manifest)

            _report(
                progress_callback,
                "editing",
                pct,
                f"[{completed}/{len(jobs)}] {job['clip_id']} | score={job['score']} | {job['product']}",
                event="clip_complete",
                clip_id=job["clip_id"],
                clip_status=clip_status,
                clips_total=len(jobs),
                clips_completed=completed,
                clips_created=clips_created,
                clips_failed=clips_failed,
                clips_skipped=clips_skipped,
            )

            if completed % edit_log_every == 0 or completed == len(jobs):
                log.info(
                    f"    Editing progress: {completed}/{len(jobs)} done | "
                    f"created={clips_created} failed={clips_failed} skipped={clips_skipped}"
                )

    # ══════════════════════════════════════════════════════════════════════════
    # DONE
    # ══════════════════════════════════════════════════════════════════════════
    clip_scores = _score_rendered_clips(jobs, manifest, output_dir, cfg, progress_callback)
    text_model_unloaded = True
    if text_model_stage_started:
        text_model_unloaded = _finish_text_model_stage(cfg)

    if clip_scores and bool(getattr(cfg, "SCORER_VISION_ENABLED", False)):
        if text_model_unloaded or not _model_management_enabled(cfg):
            clip_scores = _score_rendered_clip_host_focus(
                clip_scores,
                manifest,
                output_dir,
                cfg,
                progress_callback,
            )
        else:
            log.warning(
                "Skipping Qwen-VL host-focus scoring because %s is still loaded; "
                "this avoids loading both large models at the same time.",
                _text_model_id(cfg),
            )

    try:
        from ffmpeg_editor import flush_highlight_phrase_config
        flush_highlight_phrase_config(cfg)
    except Exception as exc:
        log.warning(f"Could not flush learned highlight phrases: {exc}")

    total_time = time.time() - pipeline_start

    # Save manifest
    _write_json_atomic(manifest_path, manifest)
    write_stage_fingerprint(
        manifest_path,
        video_path,
        cfg,
        "ffmpeg",
        extra={"max_clips": max_clips, "cut_only": cut_only},
    )
    scores_summary_path = Path(output_dir) / "scores_summary.json" if clip_scores else None

    log.info("\n" + "=" * 70)
    log.info("PIPELINE COMPLETE")
    log.info(f"  Total time:     {_fmt_time(total_time)}")
    log.info(f"  Moments found:  {len(moments)}")
    log.info(f"  Clips created:  {clips_created}")
    log.info(f"  Clips failed:   {clips_failed}")
    log.info(f"  Clips skipped:  {clips_skipped} (already existed)")
    log.info(f"  Clips scored:   {len(clip_scores)}")
    log.info(f"  Output dir:     {output_dir}")
    log.info(f"  Manifest:       {manifest_path}")
    if scores_summary_path:
        log.info(f"  Scores:         {scores_summary_path}")
    log.info("=" * 70)

    _report(
        progress_callback,
        "done",
        100,
        f"Done! {clips_created} clips created in {_fmt_time(total_time)}",
        event="pipeline_complete",
        clips_total=len(jobs),
        clips_completed=completed,
        clips_created=clips_created,
        clips_failed=clips_failed,
        clips_skipped=clips_skipped,
        output_dir=output_dir,
        manifest_path=str(manifest_path),
        scores_summary_path=str(scores_summary_path) if scores_summary_path else None,
    )

    return {
        "clips_created": clips_created,
        "clips_failed": clips_failed,
        "clips_skipped": clips_skipped,
        "moments_found": len(moments),
        "total_time": total_time,
        "output_dir": output_dir,
        "manifest_path": str(manifest_path),
        "scores_summary_path": str(scores_summary_path) if scores_summary_path else None,
        "clips_scored": len(clip_scores),
    }


# ── Utility functions ──────────────────────────────────────────────────────────

def _sync_lm_studio_model_ids(cfg) -> None:
    text_id = _text_model_id(cfg)
    vision_id = _vision_model_id(cfg)
    if text_id:
        cfg.LM_STUDIO_MODEL = text_id
    if vision_id:
        cfg.SCORER_VISION_MODEL = vision_id


def _text_model_id(cfg) -> str:
    return str(getattr(cfg, "LM_STUDIO_MOMENT_MODEL_ID", getattr(cfg, "LM_STUDIO_MODEL", "")) or "").strip()


def _vision_model_id(cfg) -> str:
    return str(getattr(cfg, "SCORER_VISION_MODEL_ID", getattr(cfg, "SCORER_VISION_MODEL", "")) or "").strip()


def _model_management_enabled(cfg) -> bool:
    return bool(getattr(cfg, "LM_STUDIO_MODEL_MANAGEMENT_ENABLED", True))


def _model_manager_module(cfg):
    if not _model_management_enabled(cfg):
        log.info("LM Studio model management disabled; using manual model management")
        return None
    try:
        import model_manager
        return model_manager
    except Exception as exc:
        log.warning(f"LM Studio model manager unavailable; continuing with manual model management: {exc}")
        return None


def _enforce_text_model_priority_at_pipeline_start(cfg) -> None:
    manager = _model_manager_module(cfg)
    if manager is None:
        return
    text_id = _text_model_id(cfg)
    vision_id = _vision_model_id(cfg)
    if not text_id or not vision_id:
        return
    try:
        loaded_ids = manager.loaded_model_ids(cfg)
        text_loaded = any(_model_id_matches(text_id, model_id) for model_id in loaded_ids)
        vision_loaded = any(_model_id_matches(vision_id, model_id) for model_id in loaded_ids)
        if text_loaded and vision_loaded:
            log.warning(
                "Both LM Studio models are loaded at pipeline start (%s and %s). "
                "Unloading Qwen-VL so Qwen text has full VRAM priority.",
                text_id,
                vision_id,
            )
            manager.unload_model(vision_id, cfg=cfg)
            _wait_until_model_unloaded(vision_id, cfg, timeout=120)
    except Exception as exc:
        log.warning(f"Could not inspect LM Studio loaded models at startup: {exc}")


def _start_text_model_stage(cfg) -> bool:
    manager = _model_manager_module(cfg)
    if manager is None:
        return False
    model_id = _text_model_id(cfg)
    if not model_id:
        return False
    log.info("LM Studio text stage: ensuring Qwen text model is loaded: %s", model_id)
    try:
        vision_id = _vision_model_id(cfg)
        if vision_id and manager.is_model_loaded(vision_id, cfg):
            log.warning(
                "Qwen-VL is loaded before the text stage. Unloading %s before loading %s.",
                vision_id,
                model_id,
            )
            manager.unload_model(vision_id, cfg=cfg, timeout=120)
            _wait_until_model_unloaded(vision_id, cfg, timeout=120)
        manager.load_model(model_id, cfg=cfg, timeout=120)
        manager.wait_until_ready(model_id, cfg=cfg, timeout=120)
        return True
    except Exception as exc:
        log.warning(f"LM Studio text model load failed; continuing without crashing: {exc}")
        return True


def _finish_text_model_stage(cfg) -> bool:
    manager = _model_manager_module(cfg)
    if manager is None:
        return True
    model_id = _text_model_id(cfg)
    if not model_id:
        return True
    log.info("LM Studio text stage complete: unloading Qwen text model: %s", model_id)
    try:
        manager.unload_model(model_id, cfg=cfg, timeout=120)
    except Exception as exc:
        log.warning(f"LM Studio text model unload failed: {exc}")
    return _wait_until_model_unloaded(model_id, cfg, timeout=120)


def _start_vision_model_stage(cfg) -> bool:
    if not bool(getattr(cfg, "SCORER_VISION_ENABLED", False)):
        return False
    manager = _model_manager_module(cfg)
    if manager is None:
        return True
    text_id = _text_model_id(cfg)
    if text_id and manager.is_model_loaded(text_id, cfg):
        log.warning("Refusing to load Qwen-VL because text model %s is still loaded.", text_id)
        return False
    model_id = _vision_model_id(cfg)
    if not model_id:
        return False
    log.info("LM Studio vision stage: loading Qwen-VL model: %s", model_id)
    try:
        loaded = manager.load_model(model_id, cfg=cfg, timeout=120)
        ready = manager.wait_until_ready(model_id, cfg=cfg, timeout=120) if loaded else False
        return bool(loaded and ready)
    except Exception as exc:
        log.warning(f"LM Studio vision model load failed; continuing without crashing: {exc}")
        return False


def _finish_vision_model_stage(cfg) -> bool:
    manager = _model_manager_module(cfg)
    if manager is None:
        return True
    model_id = _vision_model_id(cfg)
    if not model_id:
        return True
    log.info("LM Studio vision stage complete: unloading Qwen-VL model: %s", model_id)
    try:
        manager.unload_model(model_id, cfg=cfg, timeout=120)
    except Exception as exc:
        log.warning(f"LM Studio vision model unload failed: {exc}")
    return _wait_until_model_unloaded(model_id, cfg, timeout=120)


def _wait_until_model_unloaded(model_id: str, cfg, timeout: float) -> bool:
    manager = _model_manager_module(cfg)
    if manager is None:
        return True
    deadline = time.time() + max(1.0, float(timeout or 120.0))
    while time.time() < deadline:
        try:
            if not manager.is_model_loaded(model_id, cfg):
                log.info("LM Studio model unloaded: %s", model_id)
                return True
        except Exception as exc:
            log.warning(f"Could not verify model unload for {model_id}: {exc}")
            return False
        time.sleep(2.0)
    log.warning("Timed out waiting for LM Studio model to unload: %s", model_id)
    return False


def _model_id_matches(left: str, right: str) -> bool:
    lhs = str(left or "").strip().casefold()
    rhs = str(right or "").strip().casefold()
    if not lhs or not rhs:
        return False
    return lhs == rhs or lhs.split("/")[-1] == rhs.split("/")[-1]


def _fmt_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h}h {m}m {s}s"
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"


def _safe_filename(text: str) -> str:
    import re
    text = re.sub(r'[<>:"/\\|?*\n\r]', '', text)
    text = re.sub(r'\s+', '_', text.strip())
    return text or "clip"


def _report(callback, stage: str, pct: int, message: str, **payload):
    if callback:
        if payload:
            try:
                callback(stage, pct, message, **payload)
                return
            except TypeError:
                pass
        callback(stage, pct, message)


def _write_json_atomic(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _validate_startup(video_path: str, output_dir: str, cfg, skip_moments: bool, skip_vision: bool) -> None:
    errors = []
    if not _command_available("ffmpeg"):
        errors.append("FFmpeg is not accessible on PATH")
    if not _command_available("ffprobe"):
        errors.append("FFprobe is not accessible on PATH")
    if not _source_has_audio(video_path):
        errors.append(f"Input video has no audio stream: {video_path}")
    if not skip_moments and not _lm_studio_responding(cfg):
        errors.append(f"LM Studio is not responding at {getattr(cfg, 'LM_STUDIO_BASE_URL', '')}")
    if not skip_vision and not Path(getattr(cfg, "YOLO_WEIGHTS", "")).exists():
        errors.append(f"YOLO weights not found: {getattr(cfg, 'YOLO_WEIGHTS', '')}")
    free_bytes = shutil.disk_usage(output_dir).free
    if free_bytes < 10 * 1024**3:
        errors.append(f"Output disk has less than 10GB free: {output_dir}")
    if errors:
        message = "Startup validation failed:\n" + "\n".join(f"  - {item}" for item in errors)
        log.error(message)
        raise RuntimeError(message)


def _command_available(command: str) -> bool:
    try:
        result = subprocess.run(
            [command, "-version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return result.returncode == 0
    except Exception:
        return False


def _source_has_audio(video_path: str) -> bool:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "csv=p=0",
                video_path,
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        return result.returncode == 0 and bool((result.stdout or "").strip())
    except Exception:
        return False


def _lm_studio_responding(cfg) -> bool:
    try:
        from urllib.request import Request, urlopen

        base_url = str(getattr(cfg, "LM_STUDIO_BASE_URL", "")).rstrip("/")
        request = Request(
            f"{base_url}/models",
            headers={"Authorization": f"Bearer {getattr(cfg, 'LM_STUDIO_API_KEY', 'lm-studio')}"},
        )
        with urlopen(request, timeout=5) as response:
            return 200 <= int(response.status) < 500
    except Exception as exc:
        log.error(f"LM Studio startup check failed: {exc}")
        return False


# ── CLI entry point ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="PROYA Livestream → TikTok Clips Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--video", type=str, help="Path to input livestream video")
    parser.add_argument("--skip-transcribe", action="store_true", help="Use cached transcript")
    parser.add_argument("--skip-moments", action="store_true", help="Use cached moments")
    parser.add_argument("--skip-vision", action="store_true", help="Skip YOLO product scan")
    parser.add_argument("--cut-only", action="store_true", help="Cut clips without editing")
    parser.add_argument("--max-clips", type=int, default=None, help="Max number of clips to process")
    parser.add_argument("--min-score", type=float, default=None, help="Minimum LLM score (1-10)")
    parser.add_argument("--force-rescore", action="store_true", help="Bypass post-render score cache")
    parser.add_argument(
        "--output-tag",
        type=str,
        default=None,
        help="Write clips to a new output folder suffix while reusing cached working data",
    )
    parser.add_argument(
        "--working-tag",
        type=str,
        default=None,
        help="Write caches to a new working folder suffix so transcript/moments/YOLO redo from scratch",
    )
    parser.add_argument(
        "--redo-tag",
        type=str,
        default=None,
        help="Convenience tag that applies to both working and output folders for a true full redo",
    )
    parser.add_argument("--train-yolo", action="store_true", help="Train YOLO on your product dataset")
    parser.add_argument("--test-lm-studio", action="store_true", help="Test LM Studio connection")
    parser.add_argument(
        "--preview-corrections", action="store_true",
        help="Show what word corrections would be applied to a cached transcript (use with --video)"
    )
    parser.add_argument(
        "--preview-ba", action="store_true",
        help="List before/after images found in BEFORE_AFTER_DIR"
    )
    parser.add_argument(
        "--setup-sfx", action="store_true",
        help="Create SFX folders and show their status"
    )

    args = parser.parse_args()

    # ── Train YOLO ────────────────────────────────────────────────────────────
    if args.train_yolo:
        import config as cfg
        from vision_scanner import train_model
        log.info("Starting YOLO training...")
        train_model(cfg)
        return

    # ── Test LM Studio ────────────────────────────────────────────────────────
    if args.test_lm_studio:
        _test_lm_studio()
        return

    # ── Preview word corrections ──────────────────────────────────────────────
    if args.preview_corrections:
        if not args.video:
            print("Error: --preview-corrections requires --video")
            sys.exit(1)
        import config as cfg
        import json
        from word_corrector import preview_corrections
        working_dir = str(Path(cfg.WORKING_DIR) / Path(args.video).stem)
        transcript_path = Path(working_dir) / "transcript.json"
        if not transcript_path.exists():
            print(f"No cached transcript found at {transcript_path}")
            print("Run the pipeline once (or just transcription) first.")
            sys.exit(1)
        with open(transcript_path, encoding="utf-8") as f:
            transcript = json.load(f)
        examples = preview_corrections(transcript, cfg, max_examples=30)
        if not examples:
            print("✓ No corrections needed — transcript looks clean!")
        else:
            print(f"\n{'='*60}")
            print(f"WORD CORRECTIONS PREVIEW ({len(examples)} examples found)")
            print(f"{'='*60}")
            for ex in examples:
                t = ex['time']
                print(f"\n  t={t:.1f}s")
                print(f"  BEFORE: {ex['original']}")
                print(f"  AFTER:  {ex['corrected']}")
        return

    # ── Preview before/after images ───────────────────────────────────────────
    if args.preview_ba:
        import config as cfg
        from pathlib import Path
        ba_dir = Path(cfg.BEFORE_AFTER_DIR)
        if not ba_dir.exists():
            print(f"Folder not found: {ba_dir}")
            print(f"Create it and put your before/after images there.")
        else:
            exts = {".jpg", ".jpeg", ".png", ".webp"}
            imgs = [p for p in ba_dir.iterdir() if p.suffix.lower() in exts]
            print(f"\n✓ Found {len(imgs)} images in {ba_dir}:")
            for img in sorted(imgs):
                size_kb = img.stat().st_size // 1024
                print(f"  {img.name}  ({size_kb} KB)")
        return

    # ── Setup SFX folders ─────────────────────────────────────────────────────
    if args.setup_sfx:
        import config as cfg
        from sfx_player import create_sfx_folders
        create_sfx_folders(cfg)
        return

    if not args.video:
        parser.print_help()
        print("\nError: --video is required unless using --train-yolo, --test-lm-studio, --preview-ba, or --setup-sfx")
        sys.exit(1)

    output_tag = args.output_tag
    working_tag = args.working_tag
    if args.redo_tag:
        output_tag = args.redo_tag
        working_tag = args.redo_tag

    run_pipeline(
        video_path=args.video,
        skip_transcribe=args.skip_transcribe,
        skip_moments=args.skip_moments,
        skip_vision=args.skip_vision,
        cut_only=args.cut_only,
        max_clips=args.max_clips,
        min_score=args.min_score,
        force_rescore=args.force_rescore,
        output_tag=output_tag,
        working_tag=working_tag,
    )


def _test_lm_studio():
    """Quick test to verify LM Studio is running and responding."""
    import config as cfg

    log.info(f"Testing LM Studio at {cfg.LM_STUDIO_BASE_URL}...")
    try:
        from openai import OpenAI
        client = OpenAI(base_url=cfg.LM_STUDIO_BASE_URL, api_key=cfg.LM_STUDIO_API_KEY)

        response = client.chat.completions.create(
            model=cfg.LM_STUDIO_MODEL,
            messages=[
                {"role": "user", "content": "Balas hanya dengan 'OK' jika kamu bisa mendengar saya."}
            ],
            max_tokens=10,
            timeout=30,
        )
        reply = response.choices[0].message.content.strip()
        log.info(f"✓ LM Studio is working! Response: '{reply}'")
        log.info(f"  Model: {response.model}")
    except Exception as e:
        log.error(f"✗ LM Studio connection failed: {e}")
        log.error("Make sure LM Studio is running and a model is loaded in the Local Server tab.")
        sys.exit(1)


if __name__ == "__main__":
    main()
