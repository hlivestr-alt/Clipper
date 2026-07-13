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
import copy
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from bisect import bisect_left, bisect_right
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime
from pathlib import Path

from clipper_app.application.logging_utils import (
    LockedSizeRotatingFileHandler,
    PIPELINE_LOG_BACKUP_COUNT,
    PIPELINE_LOG_MAX_BYTES,
)
from stage_cache import stage_fingerprint, stage_fingerprint_matches, write_stage_fingerprint

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

# ── Configure logging ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        LockedSizeRotatingFileHandler(
            "pipeline.log",
            max_bytes=PIPELINE_LOG_MAX_BYTES,
            backup_count=PIPELINE_LOG_BACKUP_COUNT,
            encoding="utf-8",
        ),
    ],
)
log = logging.getLogger("proya.main")


EXPORT_BATCH_ASYNC_ENV = "PROYA_QUEUE_EXPORT_PACKAGING_ASYNC"
EXPORT_BATCH_TIMEOUT_ENV = "PROYA_EXPORT_BATCH_TIMEOUT_SECONDS"
_EXPORT_BATCH_PACKAGING_LOCK = threading.Lock()


class PipelinePaused(RuntimeError):
    pass


class _RuntimeConfig:
    def __init__(self, base, overrides: dict | None = None):
        object.__setattr__(self, "_base", base)
        object.__setattr__(self, "_overrides", dict(overrides or {}))

    def __getattr__(self, name: str):
        overrides = object.__getattribute__(self, "_overrides")
        if name in overrides:
            return overrides[name]
        return getattr(object.__getattribute__(self, "_base"), name)

    def __setattr__(self, name: str, value) -> None:
        if name in {"_base", "_overrides"}:
            object.__setattr__(self, name, value)
        else:
            object.__getattribute__(self, "_overrides")[name] = value


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

    clip_words = job.get("clip_words")
    if clip_words is None:
        clip_words = get_words_for_clip(transcript_words, job["start"], job["end"])

    compliance_result = _prepare_job_compliance(job, clip_words, cfg)
    if compliance_result is not None:
        if compliance_result.get("blocked"):
            return {
                "clip_id": job["clip_id"],
                "status": "compliance_blocked",
                "output_filename": job["output_filename"],
                "manifest": _build_manifest_row(job, 0, "compliance_blocked"),
            }
        try:
            from compliance_checker import apply_compliance_to_words

            clip_words = apply_compliance_to_words(clip_words, compliance_result)
        except Exception as exc:
            log.warning(f"Compliance subtitle auto-fix failed for {job['clip_id']}: {exc}")

    clip_product_events = job.get("clip_product_events")
    if clip_product_events is None:
        clip_product_events = get_events_for_clip(product_events, job["start"], job["end"])

    silence_plan = _build_job_silence_trim_plan(job, clip_words, cfg)
    if silence_plan.get("trimmed"):
        from silence_trimmer import (
            remap_events_to_compacted_timeline,
            remap_words_to_compacted_timeline,
        )

        clip_words = remap_words_to_compacted_timeline(clip_words, silence_plan)
        clip_product_events = remap_events_to_compacted_timeline(clip_product_events, silence_plan)

    job["clip_words"] = clip_words
    job["clip_product_events"] = clip_product_events

    variant = job["moment"].get("_variant", None)
    visual_mode = str(getattr(variant, "visual_mode", "host") or "host") if variant is not None else "host"
    broll_audio_visual = visual_mode == "broll_audio"
    speed_ramp = (
        1.0
        if broll_audio_visual
        else getattr(variant, "speed_ramp", 1.0) if variant is not None else 1.0
    )
    _set_job_rendered_duration(job, speed_ramp)

    force_render_existing = bool(job.get("force_render_existing"))
    if Path(output_path).exists() and not force_render_existing:
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
    if Path(output_path).exists() and force_render_existing:
        try:
            Path(output_path).unlink()
        except OSError as exc:
            log.warning(f"Could not replace stale output for {job['clip_id']}: {exc}")
            return {
                "clip_id": job["clip_id"],
                "status": "failed",
                "output_filename": job["output_filename"],
                "manifest": _build_manifest_row(job, 0, "failed"),
            }

    variant_baked = False
    should_bake_variant = (
        variant is not None
        and not broll_audio_visual
        and bool(getattr(cfg, "VARIANT_FFMPEG_BAKE", True))
    )
    if silence_plan.get("trimmed"):
        from silence_trimmer import cut_raw_clip_with_silence_plan

        cut_ok = cut_raw_clip_with_silence_plan(
            video_path,
            job["start"],
            raw_path,
            silence_plan.get("kept_ranges") or [],
            variant if should_bake_variant else None,
            cfg,
        )
        variant_baked = bool(should_bake_variant)
    elif should_bake_variant:
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
            "manifest": _build_manifest_row(job, len(clip_product_events), "ok"),
        }

    # Apply variant style overrides (font/color/zoom/y-pos) to cfg
    if variant is not None:
        try:
            from variation_engine import apply_variant_to_cfg
            edit_cfg = apply_variant_to_cfg(cfg, variant)
            setattr(edit_cfg, "_variant_transforms_baked", variant_baked)
            if broll_audio_visual:
                setattr(edit_cfg, "_speed_ramp", 1.0)
                setattr(edit_cfg, "_mirror", False)
                setattr(edit_cfg, "_crop_x_offset", 0.0)
        except ImportError:
            edit_cfg = cfg
    else:
        edit_cfg = cfg

    mirror = bool(getattr(variant, "mirror", False)) if variant is not None else False
    crop_x_offset = float(getattr(variant, "crop_x_offset", 0.0)) if variant is not None else 0.0
    if not broll_audio_visual and (mirror or abs(crop_x_offset) > 0.005):
        clip_product_events = _remap_events_for_spatial_variant(
            clip_product_events,
            mirror=mirror,
            crop_x_offset=crop_x_offset,
        )

    if abs(speed_ramp - 1.0) > 0.02:
        clip_words = _remap_words_for_speed_ramp(clip_words, speed_ramp)
        clip_product_events = _remap_events_for_speed_ramp(clip_product_events, speed_ramp)
        job["clip_words"] = clip_words
        job["clip_product_events"] = clip_product_events

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


def _ensure_job_hook_payload(job: dict) -> dict:
    if isinstance(job.get("hook_payload"), dict):
        return job["hook_payload"]
    try:
        from hook_text import ensure_hook_payload

        payload = ensure_hook_payload(job["moment"])
    except Exception:
        moment = job.get("moment", {})
        payload = {
            "headline": str(moment.get("hook") or "").strip(),
            "subtext": "",
            "cta": "",
        }
    job["hook_payload"] = payload
    return payload


def _build_job_silence_trim_plan(job: dict, clip_words: list, cfg) -> dict:
    try:
        from silence_trimmer import build_silence_trim_plan

        plan = build_silence_trim_plan(
            clip_words,
            float(job["end"]) - float(job["start"]),
            cfg,
        )
    except Exception as exc:
        log.warning(f"Silence trim planning failed for {job['clip_id']}: {exc}")
        plan = {
            "trimmed": False,
            "skip_reason": "word_timing_invalid",
            "removed_seconds": 0.0,
            "rendered_duration": round(float(job["end"]) - float(job["start"]), 6),
            "silence_ranges": [],
            "kept_ranges": [],
        }
    job["silence_trim_plan"] = plan
    return plan


def _set_job_rendered_duration(job: dict, speed_ramp: float) -> None:
    plan = job.get("silence_trim_plan") or {}
    original_duration = float(job["end"]) - float(job["start"])
    duration = float(plan.get("rendered_duration") or original_duration)
    if abs(float(speed_ramp or 1.0) - 1.0) > 0.02:
        duration = duration / max(0.01, float(speed_ramp))
    job["rendered_duration"] = round(max(0.0, duration), 6)


def _prepare_job_compliance(job: dict, clip_words: list, cfg) -> dict | None:
    if not bool(getattr(cfg, "COMPLIANCE_ENABLED", True)):
        return None
    try:
        from compliance_checker import (
            apply_compliance_to_hook_payload,
            check_compliance,
            compliance_path_for_clip,
            should_block_result,
            write_compliance_result,
        )
    except Exception as exc:
        log.warning(f"Compliance checker unavailable; failing closed for {job.get('clip_id')}: {exc}")
        result = _compliance_unavailable_result(exc)
        job["compliance_result"] = result
        return result

    try:
        result = copy.deepcopy(job.get("compliance_result")) if job.get("compliance_result") else None
        hook_payload = _ensure_job_hook_payload(job)
        if result is None:
            result = check_compliance(
                clip_words,
                job.get("product", "general"),
                hook_text=hook_payload,
                cfg=cfg,
            )

        result["blocked"] = should_block_result(result, cfg)
        if not result.get("blocked"):
            patched_hook = apply_compliance_to_hook_payload(hook_payload, result)
            if patched_hook != hook_payload:
                job["moment"]["hook_overlay"] = patched_hook
                job["moment"]["hook"] = patched_hook.get("headline", job["moment"].get("hook", ""))
                job["hook_payload"] = patched_hook
        compliance_path = compliance_path_for_clip(job["output_path"], job["clip_id"])
        write_compliance_result(compliance_path, result)
        job["compliance_result"] = result
        job["compliance_json_path"] = _relative_to_output_path(compliance_path, Path(job["output_path"]))
    except Exception as exc:
        log.warning(f"Compliance check failed closed for {job.get('clip_id')}: {exc}")
        result = _compliance_unavailable_result(exc)
        job["compliance_result"] = result

    if result.get("blocked"):
        violations = [
            str(item.get("original_text") or "")
            for item in result.get("violations", [])
            if isinstance(item, dict) and item.get("severity") == "high"
        ]
        log.warning(
            "    Compliance blocked %s: %s",
            job["clip_id"],
            "; ".join(violations[:5]) or result.get("compliance_summary", ""),
        )
    elif result.get("violation_count"):
        log.info(
            "    Compliance flagged %s: violations=%s auto_fixed=%s",
            job["clip_id"],
            result.get("violation_count", 0),
            result.get("auto_fixed", False),
        )
    return result


def _compliance_unavailable_result(exc: Exception) -> dict:
    return {
        "schema_version": 1,
        "passed": False,
        "blocked": True,
        "violation_count": 1,
        "violations": [
            {
                "original_text": "compliance_unavailable",
                "violation_type": "compliance_unavailable",
                "severity": "high",
                "suggested_replacement": "",
                "position": {"start": 0, "end": 0},
                "source": "system",
            }
        ],
        "auto_fixed": False,
        "compliance_summary": f"Compliance unavailable; clip blocked: {exc}",
        "source": "system_fail_closed",
        "qwen_called": False,
    }


def _build_manifest_row(job: dict, product_event_count: int, status: str) -> dict:
    moment = job["moment"]
    duration = round(float(job["end"]) - float(job["start"]), 1)
    row = {
        "clip_id": job["clip_id"],
        "version_dir": job.get("version_dir") or "",
        "output_file": job.get("output_relative_path") or job["output_filename"],
        "start": job["start"],
        "end": job["end"],
        "duration": duration,
        "score": job["score"],
        "hook": moment.get("hook", ""),
        "hook_overlay": moment.get("hook_overlay", {}),
        "product": job["product"],
        "clip_type": job["clip_type"],
        "reason": moment.get("reason", ""),
        "product_events": product_event_count,
        "status": status,
        "settings_revision": str(job.get("settings_revision") or "unknown"),
    }
    silence_plan = job.get("silence_trim_plan") or {}
    row["silence_trimmed"] = bool(silence_plan.get("trimmed", False))
    row["silence_trim_skip_reason"] = str(silence_plan.get("skip_reason") or "")
    row["silence_removed_seconds"] = round(float(silence_plan.get("removed_seconds") or 0.0), 3)
    row["silence_ranges"] = silence_plan.get("silence_ranges") or []
    row["rendered_duration"] = round(float(job.get("rendered_duration", duration) or duration), 3)
    compliance_result = job.get("compliance_result")
    if isinstance(compliance_result, dict):
        row["compliance_passed"] = bool(compliance_result.get("passed", False))
        row["violation_count"] = int(compliance_result.get("violation_count") or 0)
        row["auto_fixed"] = bool(compliance_result.get("auto_fixed", False))
        row["compliance_blocked"] = bool(compliance_result.get("blocked", False))
        row["compliance_summary"] = str(compliance_result.get("compliance_summary") or "")
        if job.get("compliance_json_path"):
            row["compliance_file"] = job["compliance_json_path"]
    variant = moment.get("_variant")
    if variant is not None:
        row.update({
            "variant_profile_revision": str(getattr(variant, "profile_revision", "") or ""),
            "variant_name": str(getattr(variant, "display_name", "") or ""),
            "variant_id": str(getattr(variant, "variant_id", "") or ""),
            "variant_index": int(getattr(variant, "variant_index", 0) or 0),
            "hook_type": str(getattr(variant, "hook_type", "text") or "text"),
            "visual_mode": str(getattr(variant, "visual_mode", "host") or "host"),
            "random_broll_enabled": bool(getattr(variant, "random_broll_enabled", False)),
            "font_id": str(getattr(variant, "font_id", "") or getattr(variant, "font_subtitle", "") or ""),
            "subtitle_position": str(getattr(variant, "subtitle_position", "") or ""),
            "subtitle_y_frac": float(getattr(variant, "subtitle_y_frac", getattr(variant, "subtitle_y_pos", 0.80)) or 0.80),
            "color_grade": str(getattr(variant, "color_grade", "") or ""),
            "bgm_mode": str(getattr(variant, "bgm_mode", "auto") or "auto"),
            "sfx_enabled": bool(getattr(variant, "sfx_enabled", True)),
            "zoom_intensity": str(getattr(variant, "zoom_intensity", "normal") or "normal"),
            "product_zoom_enabled": bool(getattr(variant, "product_zoom_enabled", True)),
            "subtitle_enabled": bool(getattr(variant, "subtitle_enabled", True)),
            "letterbox_enabled": bool(getattr(variant, "letterbox_enabled", False)),
            "letterbox_top_frac": float(getattr(variant, "letterbox_top_frac", 0.0) or 0.0),
            "letterbox_bottom_frac": float(getattr(variant, "letterbox_bottom_frac", 0.0) or 0.0),
        })
    broll_intro_path = str(getattr(variant, "broll_intro_path", "") or "") if variant is not None else ""
    if broll_intro_path:
        row["broll_intro"] = True
        row["broll_intro_file"] = broll_intro_path
        row["broll_intro_duration"] = float(getattr(variant, "broll_intro_duration", 0.0) or 0.0)
        row["broll_intro_product"] = str(getattr(variant, "broll_intro_product", "") or "")
    transitional_hook_path = str(getattr(variant, "transitional_hook_path", "") or "") if variant is not None else ""
    if transitional_hook_path:
        row["transitional_hook"] = True
        row["transitional_hook_file"] = transitional_hook_path
    product_broll_render = moment.get("_product_broll_render")
    if isinstance(product_broll_render, dict):
        row["product_broll_visual"] = bool(product_broll_render.get("active", False))
        row["product_broll_fallback"] = bool(product_broll_render.get("fallback", False))
        row["product_broll_reason"] = str(product_broll_render.get("reason") or "")
        row["product_broll_product"] = str(product_broll_render.get("product_key") or "")
        row["product_broll_folder"] = str(product_broll_render.get("folder") or "")
        row["product_broll_clip_count"] = int(product_broll_render.get("clip_count") or 0)
    random_broll_render = moment.get("_random_product_broll_render")
    if isinstance(random_broll_render, dict):
        row["random_product_broll"] = bool(random_broll_render.get("active", False))
        row["random_product_broll_fallback"] = bool(random_broll_render.get("fallback", False))
        row["random_product_broll_reason"] = str(random_broll_render.get("reason") or "")
        row["random_product_broll_product"] = str(random_broll_render.get("product_key") or "")
        row["random_product_broll_folder"] = str(random_broll_render.get("folder") or "")
        row["random_product_broll_clip_count"] = int(random_broll_render.get("clip_count") or 0)
        row["random_product_broll_segments"] = list(random_broll_render.get("segments") or [])
    return row


def _relative_to_output_path(path: str | Path, output_path: Path) -> str:
    target = Path(path)
    output_root = output_path.parent.parent if output_path.parent.name.startswith("v") else output_path.parent
    try:
        return str(target.resolve().relative_to(output_root.resolve())).replace("\\", "/")
    except Exception:
        return str(target)


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


def _attach_detected_product_context_to_moments(moments: list, product_events: list) -> int:
    if not moments or not product_events:
        return 0
    try:
        from product_broll import resolve_moment_product_key, resolve_product_events_key
    except Exception as exc:
        log.debug("Product B-roll resolver unavailable for detected product fallback: %s", exc)
        return 0

    event_index = _build_clip_event_index(product_events)
    attached = 0
    for moment in moments:
        if not isinstance(moment, dict) or resolve_moment_product_key(moment):
            continue
        try:
            start = float(moment.get("start", 0.0) or 0.0)
            end = float(moment.get("end", start) or start)
        except (TypeError, ValueError):
            continue
        product_key = resolve_product_events_key(_get_events_for_clip_indexed(event_index, start, end))
        if not product_key:
            continue
        moment["_detected_product_key"] = product_key
        moment["_detected_product_source"] = "yolo"
        attached += 1

    if attached:
        log.info("Attached YOLO product fallback for %s moment(s) without text product category", attached)
    return attached


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


def _attach_precomputed_compliance(jobs: list, cfg) -> None:
    if not bool(getattr(cfg, "COMPLIANCE_ENABLED", True)):
        return
    try:
        from compliance_checker import check_compliance, transcript_to_text_with_spans
    except Exception as exc:
        log.warning(f"Compliance checker unavailable; skipping pre-scan: {exc}")
        return

    result_cache: dict[tuple, dict] = {}
    scanned = 0
    flagged = 0
    qwen_calls = 0
    for job in jobs:
        clip_words = job.get("clip_words") or []
        hook_payload = _ensure_job_hook_payload(job)
        transcript_text, _spans = transcript_to_text_with_spans(clip_words)
        key = (
            round(float(job.get("start", 0.0)), 3),
            round(float(job.get("end", 0.0)), 3),
            str(job.get("product", "general")).casefold(),
            transcript_text,
            str(hook_payload),
        )
        result = result_cache.get(key)
        if result is None:
            result = check_compliance(
                clip_words,
                job.get("product", "general"),
                hook_text=hook_payload,
                cfg=cfg,
            )
            result_cache[key] = result
            scanned += 1
            flagged += 1 if result.get("violation_count") else 0
            qwen_calls += 1 if result.get("qwen_called") else 0
        job["compliance_result"] = copy.deepcopy(result)

    log.info(
        "  Compliance pre-scan: %s unique transcript(s), %s flagged, %s Qwen call(s)",
        scanned,
        flagged,
        qwen_calls,
    )


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
        if not row or row.get("status") in {"failed", "compliance_blocked"}:
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
                "compliance_passed": row.get("compliance_passed"),
                "violation_count": row.get("violation_count"),
                "auto_fixed": row.get("auto_fixed"),
                "compliance_blocked": row.get("compliance_blocked"),
                "compliance_summary": row.get("compliance_summary", ""),
                "compliance_file": row.get("compliance_file", ""),
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
        finalize_scores = not bool(getattr(cfg, "SCORER_VISION_ENABLED", False))
        artifacts = write_score_artifacts(
            scores,
            output_dir,
            groups=groups,
            optimization_stats=stats,
            cfg=cfg,
            finalize=finalize_scores,
        )
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
            "  Merged Qwen text calls saved: %s (previous=%s actual_http=%s)",
            stats.get("saved_text_qwen_calls", 0),
            stats.get("previous_text_qwen_calls", 0),
            stats.get("actual_text_qwen_calls", 0),
        )
        log.info(f"  Scores summary: {artifacts.get('summary_path')}")
        if artifacts.get("scores_report_path"):
            log.info(f"  Scores report:  {artifacts.get('scores_report_path')}")
        elif not finalize_scores:
            log.info("  Text-only scores saved as draft; final tier sorting waits for host-focus scoring")
        _apply_score_sort_moves_to_manifest(manifest, artifacts)

    return scores


def _score_rendered_clip_host_focus(
    scores: list,
    manifest: list,
    output_dir: str,
    cfg,
    progress_callback=None,
) -> list:
    if not scores or not bool(getattr(cfg, "SCORER_VISION_ENABLED", False)):
        log.info("Host-focus vision scoring disabled or no text scores; Qwen-VL not needed")
        return scores

    log.info("\n-- STAGE 6: HOST FOCUS VISION SCORING (Qwen-VL) --------------------------")
    _report(progress_callback, "scoring", 99, "Scoring host focus with Qwen-VL...")
    vision_ready = _start_vision_model_stage(
        cfg,
        active_stage="stage 6 host focus vision scoring",
    )
    if not vision_ready and _model_management_enabled(cfg):
        _finish_vision_model_stage(cfg)
        raise RuntimeError("Qwen-VL did not become ready for host-focus scoring")

    try:
        from clip_scorer import apply_host_focus_vision_scores, write_score_artifacts
    except Exception as exc:
        raise RuntimeError(f"Clip scorer vision stage unavailable: {exc}") from exc

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
            optimization_stats=_merge_score_optimization_stats(output_dir, vision_stats),
            cfg=cfg,
        )
        log.info(
            "  Host-focus vision scored %s/%s base group(s)",
            vision_stats.get("vision_scored_groups", 0),
            vision_stats.get("vision_base_group_count", 0),
        )
        log.info(
            "  Actual Qwen vision HTTP calls: %s",
            vision_stats.get("actual_vision_qwen_calls", 0),
        )
        log.info(f"  Updated scores summary: {artifacts.get('summary_path')}")
        _apply_score_sort_moves_to_manifest(manifest, artifacts)
        return updated_scores
    finally:
        _finish_vision_model_stage(cfg, active_stage="stage 6 host focus vision scoring cleanup")


def _merge_score_optimization_stats(output_dir: str, vision_stats: dict) -> dict:
    existing = _read_score_optimization_stats(Path(output_dir) / "scores_summary.json")
    merged = dict(existing) if isinstance(existing, dict) else {}
    merged["vision_scoring"] = vision_stats
    merged["actual_vision_qwen_calls"] = int(vision_stats.get("actual_vision_qwen_calls") or 0)
    merged.setdefault("actual_text_qwen_calls", 0)
    return merged


def _read_score_optimization_stats(summary_path: Path | None) -> dict:
    if summary_path is None or not summary_path.exists():
        return {}
    try:
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    stats = payload.get("scoring_optimization", {}) if isinstance(payload, dict) else {}
    return stats if isinstance(stats, dict) else {}


def run_module_assembly(
    assembly_date: str | None = None,
    product: str | None = None,
    module_assembly_limit: int | None = None,
    module_product_zoom: bool = False,
    progress_callback=None,
    runtime_cfg=None,
) -> dict:
    """Standalone module-library assembly run that does not depend on one source video."""
    if runtime_cfg is None:
        import config as base_cfg
    else:
        base_cfg = runtime_cfg
    from module_assembler import build_modular_assembly_jobs, render_modular_assemblies
    from module_extractor import PRODUCT_FOLDERS, canonical_product, read_library_index

    source_date_filter = _validate_cli_date(assembly_date) if assembly_date else None
    product_filter = canonical_product(product) if product else None
    if product and not product_filter:
        valid_products = ", ".join(PRODUCT_FOLDERS)
        raise ValueError(f"Invalid --product value {product!r}; expected one of: {valid_products}")

    output_date = source_date_filter or datetime.now().astimezone().date().isoformat()
    runtime_overrides = {
        "MODULE_ASSEMBLY_ENABLED": True,
        "MODULE_ASSEMBLY_OUTPUT_SUBDIR": "",
    }
    if source_date_filter:
        runtime_overrides["MODULE_ASSEMBLY_SOURCE_DATE"] = source_date_filter
    if module_assembly_limit is not None:
        runtime_overrides["MODULE_ASSEMBLY_RENDER_LIMIT"] = max(0, int(module_assembly_limit))
    if module_product_zoom:
        runtime_overrides["MODULE_PRODUCT_ZOOM_ENABLED"] = True

    cfg = _RuntimeConfig(base_cfg, runtime_overrides)
    _sync_lm_studio_model_ids(cfg)

    output_dir = Path(cfg.OUTPUT_DIR) / "modular_assembly" / output_date
    working_dir = Path(cfg.WORKING_DIR) / "modular_assembly" / output_date
    output_dir.mkdir(parents=True, exist_ok=True)
    working_dir.mkdir(parents=True, exist_ok=True)

    _report(progress_callback, "modular", 0, "Standalone module assembly started")
    log.info("=" * 70)
    log.info("PROYA STANDALONE MODULE ASSEMBLY")
    log.info("  Library:    %s", getattr(cfg, "MODULE_LIBRARY_DIR", r"D:\proya_modules"))
    log.info("  Output:     %s", output_dir)
    log.info("  Working:    %s", working_dir)
    if source_date_filter:
        log.info("  Source date:%s", source_date_filter)
    if product_filter:
        log.info("  Product:    %s", product_filter)
    if module_assembly_limit is not None:
        log.info("  Limit:      %s", max(0, int(module_assembly_limit)))
    log.info("  Zoom:       %s", "enabled" if getattr(cfg, "MODULE_PRODUCT_ZOOM_ENABLED", False) else "disabled")
    log.info("=" * 70)

    _enforce_text_model_priority_at_pipeline_start(cfg)
    pipeline_start = time.time()
    text_model_stage_started = False
    text_model_finished = False
    manifest: list[dict] = []
    manifest_path = output_dir / "manifest.json"
    scores: list[dict] = []

    try:
        if bool(getattr(cfg, "COMPLIANCE_ENABLED", True)) or bool(getattr(cfg, "SCORER_ENABLED", True)):
            text_model_stage_started = _start_text_model_stage(cfg)

        _report(progress_callback, "modular", 10, "Loading module library index...")
        index = read_library_index(cfg)
        if product_filter:
            index = _filter_module_index_for_product(index, product_filter)
        log.info(
            "Loaded module index: modules=%s updated_at=%s",
            index.get("module_count", len(index.get("modules", []) or [])),
            index.get("updated_at", ""),
        )

        _report(progress_callback, "modular", 20, "Building modular assembly jobs...")
        jobs = build_modular_assembly_jobs(index, output_dir, cfg)
        log.info("Built %s modular assembly candidate(s)", len(jobs))

        _report(progress_callback, "modular", 35, "Rendering modular assemblies...")
        result = render_modular_assemblies(
            jobs,
            cfg,
            output_dir=output_dir,
            working_dir=working_dir,
            progress_callback=progress_callback,
        )
        manifest = result.get("manifest", []) if isinstance(result.get("manifest"), list) else []
        manifest_path = Path(result.get("manifest_path") or manifest_path)
        scores = result.get("scores", []) if isinstance(result.get("scores"), list) else []

        vision_scoring_requested = bool(scores and getattr(cfg, "SCORER_VISION_ENABLED", False))
        text_model_unloaded = _finish_text_model_stage_for_vision_handoff(
            cfg,
            text_model_stage_started=text_model_stage_started,
            vision_scoring_requested=vision_scoring_requested,
            active_stage="standalone module assembly text scoring",
        )
        if text_model_stage_started:
            text_model_finished = True

        if vision_scoring_requested:
            if text_model_unloaded or not _model_management_enabled(cfg):
                scores = _score_rendered_clip_host_focus(
                    scores,
                    manifest,
                    str(output_dir),
                    cfg,
                    progress_callback,
                )
                result["scores"] = scores
                _write_json_atomic(manifest_path, manifest)
            else:
                raise RuntimeError(
                    "Host-focus vision scoring cannot start because "
                    f"{_text_model_id(cfg)} is still loaded after module text scoring."
                )

        if not manifest_path.exists():
            _write_json_atomic(manifest_path, manifest)

        try:
            from compliance_checker import update_scores_summary_with_compliance

            update_scores_summary_with_compliance(output_dir, manifest)
        except Exception as exc:
            log.warning(f"Could not merge modular compliance fields into score summary: {exc}")

        scores_summary_path = output_dir / "scores_summary.json" if scores else None
        total_time = time.time() - pipeline_start
        scorer_accounting = _read_score_optimization_stats(scores_summary_path)
        log.info("\n" + "=" * 70)
        log.info("MODULE ASSEMBLY COMPLETE")
        log.info("  Total time:     %s", _fmt_time(total_time))
        log.info("  Candidates:     %s", result.get("jobs", len(jobs)))
        log.info("  Created:        %s", result.get("created", 0))
        log.info("  Failed:         %s", result.get("failed", 0))
        log.info("  Blocked:        %s", result.get("blocked", 0))
        log.info("  Scored:         %s", len(scores))
        if scorer_accounting:
            vision_stats = scorer_accounting.get("vision_scoring", {})
            if not isinstance(vision_stats, dict):
                vision_stats = {}
            log.info(
                "  Qwen HTTP calls: text=%s vision=%s",
                scorer_accounting.get("actual_text_qwen_calls", 0),
                scorer_accounting.get(
                    "actual_vision_qwen_calls",
                    vision_stats.get("actual_vision_qwen_calls", 0),
                ),
            )
        log.info("  Output dir:     %s", output_dir)
        log.info("  Manifest:       %s", manifest_path)
        if scores_summary_path:
            log.info("  Scores:         %s", scores_summary_path)
        log.info("=" * 70)

        _report(
            progress_callback,
            "done",
            100,
            f"Done! {result.get('created', 0)} modular clips created in {_fmt_time(total_time)}",
            event="module_assembly_complete",
            clips_created=result.get("created", 0),
            clips_failed=result.get("failed", 0),
            clips_blocked=result.get("blocked", 0),
            output_dir=str(output_dir),
            manifest_path=str(manifest_path),
            scores_summary_path=str(scores_summary_path) if scores_summary_path else None,
        )

        return {
            **result,
            "total_time": total_time,
            "output_dir": str(output_dir),
            "working_dir": str(working_dir),
            "manifest_path": str(manifest_path),
            "scores_summary_path": str(scores_summary_path) if scores_summary_path else None,
            "clips_scored": len(scores),
            "source_date_filter": source_date_filter,
            "product_filter": product_filter,
        }
    finally:
        if text_model_stage_started and not text_model_finished:
            try:
                _finish_text_model_stage(
                    cfg,
                    active_stage="standalone module assembly cleanup",
                    required=False,
                )
            except Exception as exc:
                log.warning(f"Could not unload text model after module assembly: {exc}")


def _run_modular_assembly(output_dir: str, working_dir: str, cfg, progress_callback=None) -> dict:
    """Run the in-pipeline modular assembly path against the current module library."""
    from module_assembler import build_and_render_from_library

    return build_and_render_from_library(
        output_dir,
        working_dir,
        cfg,
        progress_callback=progress_callback,
    )


def _filter_module_index_for_product(index: dict, product: str) -> dict:
    from module_extractor import canonical_product

    modules = [
        module
        for module in (index.get("modules", []) or [])
        if canonical_product(module.get("product")) == product
    ]
    filtered = dict(index)
    filtered["modules"] = modules
    filtered["module_count"] = len(modules)
    filtered["product_filter"] = product
    return filtered


def _validate_cli_date(value: str) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise ValueError(f"Invalid --date value {value!r}; expected YYYY-MM-DD")
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"Invalid --date value {value!r}; expected a real YYYY-MM-DD date") from exc
    return text


def _apply_score_sort_moves_to_manifest(manifest: list, artifacts: dict | None) -> None:
    tier_move = (artifacts or {}).get("tier_move", {})
    moves = {
        str(move.get("clip_id")): move
        for move in tier_move.get("moves", [])
        if isinstance(move, dict) and move.get("clip_id")
    }
    if not moves:
        return
    for row in manifest:
        if not isinstance(row, dict):
            continue
        move = moves.get(str(row.get("clip_id") or ""))
        if move and move.get("output_file"):
            row["output_file"] = move["output_file"]


def _load_manifest_rows(manifest_path: Path) -> list:
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return payload if isinstance(payload, list) else []


def _render_state_path_for_manifest(manifest_path: Path) -> Path:
    return manifest_path.with_name("render_state.json")


def _variation_profile_revision_for_render(cfg) -> str:
    try:
        from variation_profile import active_profile_revision

        return active_profile_revision(cfg)
    except Exception as exc:
        log.warning("Could not read variation profile revision for render fingerprint: %s", exc)
        return ""


def _variation_profile_uses_product_broll(cfg) -> bool:
    try:
        from variation_profile import load_active_profile

        profile = load_active_profile(cfg)
    except Exception as exc:
        log.warning("Could not inspect variation profile visual modes for render fingerprint: %s", exc)
        return False
    variants = profile.get("variants") if isinstance(profile, dict) else []
    if not isinstance(variants, list):
        return False
    return any(
        isinstance(variant, dict)
        and str(variant.get("visual_mode") or "host").strip().casefold() == "broll_audio"
        for variant in variants
    )


def _product_broll_asset_fingerprint_for_render(cfg) -> str:
    if not _variation_profile_uses_product_broll(cfg):
        return ""
    try:
        from product_broll import product_broll_asset_fingerprint

        return product_broll_asset_fingerprint(cfg)
    except Exception as exc:
        log.warning("Could not fingerprint product B-roll assets for render: %s", exc)
        return ""


def _render_fingerprint_extra(cfg, max_clips: int | None, cut_only: bool) -> dict:
    return {
        "max_clips": max_clips,
        "cut_only": cut_only,
        "variation_profile_revision": _variation_profile_revision_for_render(cfg),
        "product_broll_asset_fingerprint": _product_broll_asset_fingerprint_for_render(cfg),
    }


def _render_fingerprint(video_path: str, cfg, max_clips: int | None, cut_only: bool) -> dict:
    return stage_fingerprint(
        video_path,
        cfg,
        "ffmpeg",
        extra=_render_fingerprint_extra(cfg, max_clips, cut_only),
    )


def _load_matching_render_state(render_state_path: Path, expected_fingerprint: dict) -> dict:
    try:
        payload = json.loads(render_state_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    if payload.get("render_fingerprint") != expected_fingerprint:
        return {}
    return payload


def _manifest_rows_by_clip(manifest: list) -> dict[str, dict]:
    return {
        str(row.get("clip_id") or ""): row
        for row in manifest
        if isinstance(row, dict) and row.get("clip_id")
    }


def _completed_resume_rows(jobs: list, manifest: list, output_dir: Path) -> list[dict]:
    rows_by_clip = _manifest_rows_by_clip(manifest)
    completed_rows: list[dict] = []
    for job in jobs:
        clip_id = str(job.get("clip_id") or "")
        row = rows_by_clip.get(clip_id)
        if not row:
            continue
        status = str(row.get("status") or "").casefold()
        if status in {"failed", ""}:
            continue
        if status in {"ok", "skipped", "filtered_low_score", "filtered_low_variant"}:
            output_file = str(row.get("output_file") or "").strip()
            if not output_file or not _resolve_manifest_output_path(output_dir, output_file).exists():
                continue
        completed_rows.append(row)
    return completed_rows


def _write_render_state(
    render_state_path: Path,
    render_fingerprint: dict,
    status: str,
    clips_total: int,
    manifest: list,
    counts: dict,
    active_clip_renders: int = 0,
    last_clip_id: str | None = None,
    last_clip_status: str | None = None,
) -> None:
    completed_ids = [
        str(row.get("clip_id"))
        for row in manifest
        if isinstance(row, dict)
        and row.get("clip_id")
        and str(row.get("status") or "").casefold() not in {"failed", ""}
    ]
    payload = {
        "schema_version": 1,
        "status": status,
        "render_fingerprint": render_fingerprint,
        "clips_total": clips_total,
        "clips_completed": int(counts.get("clips_completed", 0) or 0),
        "clips_created": int(counts.get("clips_created", 0) or 0),
        "clips_failed": int(counts.get("clips_failed", 0) or 0),
        "clips_skipped": int(counts.get("clips_skipped", 0) or 0),
        "clips_blocked": int(counts.get("clips_blocked", 0) or 0),
        "active_clip_renders": max(0, int(active_clip_renders or 0)),
        "completed_clip_ids": completed_ids,
        "last_clip_id": last_clip_id,
        "last_clip_status": last_clip_status,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    _write_json_atomic(render_state_path, payload)


def _pause_requested(control_path: str | None) -> bool:
    if not control_path:
        return False
    try:
        import queue_control

        return queue_control.pause_requested(control_path)
    except Exception:
        return False


def _ordered_manifest_rows(jobs: list, rows_by_clip: dict[str, dict]) -> list:
    ordered = []
    seen = set()
    for job in jobs:
        clip_id = str(job.get("clip_id") or "")
        row = rows_by_clip.get(clip_id)
        if row:
            ordered.append(row)
            seen.add(clip_id)
    for clip_id, row in rows_by_clip.items():
        if clip_id not in seen:
            ordered.append(row)
    return ordered


def _render_clip_jobs_incremental(
    jobs: list,
    pending_jobs: list,
    manifest_rows: dict[str, dict],
    manifest_path: Path,
    render_state_path: Path,
    render_fingerprint: dict,
    video_path: str,
    transcript_words: list,
    product_events: list,
    cut_only: bool,
    cfg,
    progress_callback=None,
    control_path: str | None = None,
) -> tuple[list, dict]:
    max_workers = max(1, int(getattr(cfg, "MAX_PARALLEL_CLIPS", 6)))
    edit_log_every = max(1, int(getattr(cfg, "EDIT_LOG_EVERY_N", 25)))
    counts = {
        "clips_completed": len(manifest_rows),
        "clips_created": 0,
        "clips_failed": 0,
        "clips_skipped": 0,
        "clips_blocked": 0,
    }
    for row in manifest_rows.values():
        status = str(row.get("status") or "").casefold()
        if status == "skipped":
            counts["clips_skipped"] += 1
            counts["clips_created"] += 1
        elif status == "compliance_blocked":
            counts["clips_blocked"] += 1
        elif status and status != "failed":
            counts["clips_created"] += 1

    total_jobs = len(jobs)
    completed = int(counts["clips_completed"])
    next_job = 0
    last_clip_id = None
    last_clip_status = None

    _write_render_state(
        render_state_path,
        render_fingerprint,
        "running",
        total_jobs,
        _ordered_manifest_rows(jobs, manifest_rows),
        counts,
    )

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {}
        while next_job < len(pending_jobs) or futures:
            while (
                next_job < len(pending_jobs)
                and len(futures) < max_workers
                and not _pause_requested(control_path)
            ):
                job = pending_jobs[next_job]
                next_job += 1
                future = executor.submit(
                    _process_clip_job,
                    job,
                    video_path,
                    transcript_words,
                    product_events,
                    cut_only,
                    cfg,
                )
                futures[future] = job
                _report(
                    progress_callback,
                    "editing",
                    50 + int((completed / max(1, total_jobs)) * 45),
                    f"Rendering {job['clip_id']}...",
                    event="clip_started",
                    clip_id=job["clip_id"],
                    clips_total=total_jobs,
                    clips_completed=completed,
                    active_clip_renders=len(futures),
                    render_state_path=str(render_state_path),
                )

            if not futures:
                if next_job < len(pending_jobs) and _pause_requested(control_path):
                    break
                continue

            done, _ = wait(futures.keys(), timeout=1.0, return_when=FIRST_COMPLETED)
            if not done:
                _write_render_state(
                    render_state_path,
                    render_fingerprint,
                    "pausing" if _pause_requested(control_path) else "running",
                    total_jobs,
                    _ordered_manifest_rows(jobs, manifest_rows),
                    counts,
                    active_clip_renders=len(futures),
                    last_clip_id=last_clip_id,
                    last_clip_status=last_clip_status,
                )
                continue

            for future in done:
                job = futures.pop(future)
                completed += 1
                counts["clips_completed"] = completed
                pct = 50 + int((completed / max(1, total_jobs)) * 45)
                clip_status = "failed"

                try:
                    result = future.result()
                except Exception as exc:
                    counts["clips_failed"] += 1
                    log.error(f"    Worker failed for {job['clip_id']}: {exc}")
                    row = _build_manifest_row(job, 0, "failed")
                    manifest_rows[str(job["clip_id"])] = row
                else:
                    clip_status = result["status"]
                    if result["status"] == "skipped":
                        counts["clips_skipped"] += 1
                        counts["clips_created"] += 1
                        log.debug(f"    Already exists, skipping: {result['output_filename']}")
                    elif result["status"] == "ok":
                        counts["clips_created"] += 1
                        if getattr(cfg, "EDIT_LOG_CREATED_CLIPS", False):
                            log.info(f"    Created: {result['output_filename']}")
                    elif result["status"] == "compliance_blocked":
                        counts["clips_blocked"] += 1
                        log.warning(f"    Compliance blocked export for {job['clip_id']}")
                    else:
                        counts["clips_failed"] += 1
                        log.error(f"    Edit failed for {job['clip_id']}")

                    if result.get("manifest"):
                        manifest_rows[str(result["manifest"].get("clip_id") or job["clip_id"])] = result["manifest"]

                last_clip_id = str(job["clip_id"])
                last_clip_status = clip_status
                manifest = _ordered_manifest_rows(jobs, manifest_rows)
                _write_json_atomic(manifest_path, manifest)
                _write_render_state(
                    render_state_path,
                    render_fingerprint,
                    "pausing" if _pause_requested(control_path) else "running",
                    total_jobs,
                    manifest,
                    counts,
                    active_clip_renders=len(futures),
                    last_clip_id=last_clip_id,
                    last_clip_status=last_clip_status,
                )

                _report(
                    progress_callback,
                    "editing",
                    pct,
                    f"[{completed}/{total_jobs}] {job['clip_id']} | score={job['score']} | {job['product']}",
                    event="clip_complete",
                    clip_id=job["clip_id"],
                    clip_status=clip_status,
                    clips_total=total_jobs,
                    clips_completed=completed,
                    clips_created=counts["clips_created"],
                    clips_failed=counts["clips_failed"],
                    clips_skipped=counts["clips_skipped"],
                    clips_blocked=counts["clips_blocked"],
                    active_clip_renders=len(futures),
                    render_state_path=str(render_state_path),
                )

                if completed % edit_log_every == 0 or completed == total_jobs:
                    log.info(
                        f"    Editing progress: {completed}/{total_jobs} done | "
                        f"created={counts['clips_created']} failed={counts['clips_failed']} "
                        f"skipped={counts['clips_skipped']} blocked={counts['clips_blocked']}"
                    )

        if _pause_requested(control_path) and next_job < len(pending_jobs):
            manifest = _ordered_manifest_rows(jobs, manifest_rows)
            _write_json_atomic(manifest_path, manifest)
            _write_render_state(
                render_state_path,
                render_fingerprint,
                "paused",
                total_jobs,
                manifest,
                counts,
                active_clip_renders=0,
                last_clip_id=last_clip_id,
                last_clip_status=last_clip_status,
            )
            _report(
                progress_callback,
                "editing",
                50 + int((completed / max(1, total_jobs)) * 45),
                f"Paused after {completed}/{total_jobs} clips",
                event="render_paused",
                clips_total=total_jobs,
                clips_completed=completed,
                clips_created=counts["clips_created"],
                clips_failed=counts["clips_failed"],
                clips_skipped=counts["clips_skipped"],
                clips_blocked=counts["clips_blocked"],
                active_clip_renders=0,
                render_paused=True,
                render_state_path=str(render_state_path),
            )
            raise PipelinePaused(f"Graceful stop after {completed}/{total_jobs} clip jobs")

    manifest = _ordered_manifest_rows(jobs, manifest_rows)
    _write_render_state(
        render_state_path,
        render_fingerprint,
        "rendered",
        total_jobs,
        manifest,
        counts,
        active_clip_renders=0,
        last_clip_id=last_clip_id,
        last_clip_status=last_clip_status,
    )
    return manifest, counts


def _reuse_existing_manifest_outputs_for_jobs(jobs: list, manifest: list, output_dir: Path) -> int:
    rows_by_clip = {
        str(row.get("clip_id") or ""): row
        for row in manifest
        if isinstance(row, dict) and row.get("clip_id")
    }
    reused = 0
    for job in jobs:
        row = rows_by_clip.get(str(job.get("clip_id") or ""))
        if not row or str(row.get("status") or "").casefold() in {"failed", "compliance_blocked"}:
            continue
        output_file = str(row.get("output_file") or "").strip()
        if not output_file:
            continue
        output_path = _resolve_manifest_output_path(output_dir, output_file)
        if not output_path.exists() or not output_path.is_file():
            continue
        job["output_relative_path"] = output_file.replace("\\", "/")
        job["output_path"] = str(output_path)
        job["output_filename"] = output_path.name
        reused += 1
    return reused


def _resolve_manifest_output_path(output_dir: Path, output_file: str) -> Path:
    path = Path(str(output_file).replace("\\", "/"))
    return path if path.is_absolute() else output_dir / path


def _export_batch_timeout_seconds(cfg) -> float:
    raw_value = os.environ.get(
        EXPORT_BATCH_TIMEOUT_ENV,
        getattr(cfg, "EXPORT_BATCH_TIMEOUT_SECONDS", 900),
    )
    try:
        return max(0.0, float(raw_value))
    except (TypeError, ValueError):
        return 900.0


def _export_batch_folder_count(result: dict) -> int:
    folders = {
        str(item.get("batch_folder"))
        for item in result.get("assignments", [])
        if isinstance(item, dict) and item.get("batch_folder") is not None
    }
    return len(folders)


def _log_export_batch_success(result: dict) -> None:
    packed = int(result.get("packaged_count", 0) or 0)
    folders = _export_batch_folder_count(result)
    log.info("Export batch packaging complete: %s clips packed into %s folders", packed, folders)


def _run_export_batch_packaging(
    cfg,
    progress_callback=None,
    lock_timeout: float | None = None,
    queue_continues: bool = False,
) -> dict:
    if not bool(getattr(cfg, "EXPORT_BATCHES_ENABLED", False)):
        return {}
    try:
        from export_packager import package_export_batches
    except Exception as exc:
        if queue_continues:
            log.warning("Export batch packaging failed: %s — queue continues", exc)
        else:
            log.warning(f"Export batch packager unavailable; skipping affiliate batches: {exc}")
        return {}

    output_root = Path(getattr(cfg, "OUTPUT_DIR", r"D:\output_clips"))
    batch_size = int(getattr(cfg, "EXPORT_BATCH_SIZE", 30) or 30)
    if progress_callback is not None:
        _report(
            progress_callback,
            "export_batches",
            99,
            "Packaging export-ready clips into affiliate folders...",
        )
    acquired = False
    try:
        if lock_timeout is None:
            _EXPORT_BATCH_PACKAGING_LOCK.acquire()
            acquired = True
        else:
            acquired = _EXPORT_BATCH_PACKAGING_LOCK.acquire(timeout=lock_timeout)
            if not acquired:
                raise TimeoutError(
                    f"another export batch packaging job is still running after {lock_timeout:.0f}s"
                )
        result = package_export_batches(output_root, cfg=cfg, batch_size=batch_size)
    except Exception as exc:
        if queue_continues:
            log.warning("Export batch packaging failed: %s — queue continues", exc)
        else:
            log.warning(f"Could not package export-ready clips into affiliate folders: {exc}")
        return {"error_count": 1, "errors": [str(exc)]}
    finally:
        if acquired:
            _EXPORT_BATCH_PACKAGING_LOCK.release()

    _log_export_batch_success(result)
    log.info(
        "  Export batch packaging: eligible=%s new_unique=%s moved=%s duplicate_existing=%s duplicate_candidate=%s errors=%s",
        result.get("eligible_count", 0),
        result.get("new_unique_count", 0),
        result.get("packaged_count", 0),
        result.get("duplicate_existing_count", 0),
        result.get("duplicate_candidate_count", 0),
        result.get("error_count", 0),
    )
    if result.get("manifest_path"):
        log.info("  Export batch manifest: %s", result.get("manifest_path"))
    if result.get("legacy_batch_folder_cutoff") is not None:
        log.info("  Export batch legacy cutoff: %s", result.get("legacy_batch_folder_cutoff"))
    return result


def _start_export_batch_packaging_thread(cfg) -> threading.Thread:
    timeout = _export_batch_timeout_seconds(cfg)

    def worker() -> None:
        done = threading.Event()

        if timeout > 0:
            def watchdog() -> None:
                if not done.wait(timeout):
                    log.warning(
                        "Export batch packaging failed: timed out after %.0fs — queue continues",
                        timeout,
                    )

            threading.Thread(
                target=watchdog,
                name="export-batch-packaging-timeout-watch",
                daemon=True,
            ).start()

        try:
            _run_export_batch_packaging(
                cfg,
                progress_callback=None,
                lock_timeout=timeout if timeout > 0 else None,
                queue_continues=True,
            )
        finally:
            done.set()

    thread = threading.Thread(
        target=worker,
        name=f"export-batch-packaging-{int(time.time())}",
        daemon=True,
    )
    thread.start()
    return thread


def _package_export_batches_if_enabled(
    cfg,
    progress_callback=None,
    *,
    max_clips: int | None = None,
) -> dict:
    if not bool(getattr(cfg, "EXPORT_BATCHES_ENABLED", False)):
        return {}
    if max_clips is not None and os.environ.get(EXPORT_BATCH_ASYNC_ENV) != "1":
        log.info("Skipping export batch packaging for bounded --max-clips run")
        return {"skipped": True, "reason": "max_clips_manual_run"}
    if os.environ.get(EXPORT_BATCH_ASYNC_ENV) == "1":
        if progress_callback is not None:
            _report(
                progress_callback,
                "export_batches",
                99,
                "Export batch packaging submitted in background...",
            )
        thread = _start_export_batch_packaging_thread(cfg)
        log.info("Export batch packaging submitted in background: %s", thread.name)
        return {"async": True, "thread_name": thread.name}
    return _run_export_batch_packaging(cfg, progress_callback=progress_callback)


def _attach_score_to_manifest(row: dict, score: dict, cfg) -> None:
    row["scorer_base_clip_id"] = score.get("base_clip_id")
    row["scorer_variant_id"] = score.get("variant_id")
    row["scorer_total_score"] = score.get("total_score")
    row["scorer_content_score"] = score.get("content_score")
    row.pop("scorer_visual_score", None)
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


def _run_pipeline_impl(
    video_path: str,
    skip_transcribe: bool = False,
    skip_moments: bool = False,
    skip_vision: bool = False,
    cut_only: bool = False,
    max_clips: int = None,
    min_score: float = None,
    force_rescore: bool = False,
    extract_modules_only: bool = False,
    force_modules: bool = False,
    render_modules: bool = False,
    modular_only: bool = False,
    module_assembly_limit: int | None = None,
    module_product_zoom: bool = False,
    output_tag: str | None = None,
    working_tag: str | None = None,
    control_path: str | None = None,
    progress_callback=None,   # optional: fn(stage, pct, message, **payload)
    runtime_cfg=None,
    settings_overrides: dict | None = None,
):
    """
    Full pipeline orchestrator. All stages cache their results so you can
    safely re-run after a crash — it picks up where it left off.
    """
    if runtime_cfg is None:
        import config as base_cfg
    else:
        base_cfg = runtime_cfg

    modular_requested = bool(render_modules or modular_only)
    runtime_overrides = {
        "MODULE_ASSEMBLY_ENABLED": modular_requested,
        "MODULE_PRODUCT_ZOOM_ENABLED": False,
    }
    runtime_overrides.update(settings_overrides or {})
    if not modular_requested:
        runtime_overrides["MODULE_ASSEMBLY_RENDER_LIMIT"] = 0
    elif module_assembly_limit is not None:
        runtime_overrides["MODULE_ASSEMBLY_RENDER_LIMIT"] = max(0, int(module_assembly_limit))
    if module_product_zoom:
        runtime_overrides["MODULE_PRODUCT_ZOOM_ENABLED"] = True
    if min_score is not None:
        runtime_overrides["MIN_SCORE"] = min_score
    if force_rescore:
        runtime_overrides["SCORER_FORCE_RESCORE"] = True

    cfg = _RuntimeConfig(base_cfg, runtime_overrides)
    _sync_lm_studio_model_ids(cfg)
    module_extraction_enabled = bool(
        extract_modules_only or getattr(cfg, "MODULE_EXTRACTION_ENABLED", True)
    )
    skip_vision_for_run = bool(skip_vision or extract_modules_only or modular_only)

    # ── Validate inputs ───────────────────────────────────────────────────────
    if not Path(video_path).exists():
        raise FileNotFoundError(f"Input video not found: {video_path}")

    base_stem = Path(video_path).stem
    working_stem = _build_versioned_stem(base_stem, working_tag)
    working_dir = str(Path(cfg.WORKING_DIR) / working_stem)

    output_stem = _build_versioned_stem(base_stem, output_tag)
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

    _validate_startup(
        video_path,
        output_dir,
        cfg,
        skip_moments=skip_moments,
        skip_vision=skip_vision_for_run,
        module_extraction_enabled=module_extraction_enabled,
    )
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

    if (
        (not skip_moments)
        or module_extraction_enabled
        or bool(getattr(cfg, "SCORER_ENABLED", True))
        or bool(getattr(cfg, "COMPLIANCE_ENABLED", True))
    ):
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

    write_stage_fingerprint(Path(working_dir) / "moments.json", video_path, cfg, "llm")

    module_stats = None
    if not moments:
        skip_vision_for_run = True

    # ── Variation expansion ───────────────────────────────────────────────────
    n_variants = 1  # legacy location disabled; expansion runs after module extraction below
    if False:  # see note above
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

    if not skip_vision_for_run:
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

    if module_extraction_enabled:
        _report(progress_callback, "modules", 55, "Extracting reusable raw modules...")
        log.info("\n-- MODULE LIBRARY EXTRACTION ---------------------------------------------")
        from module_extractor import extract_modules

        t0 = time.time()
        module_stats = extract_modules(
            video_path=video_path,
            transcript=transcript,
            moments=moments,
            working_dir=working_dir,
            cfg=cfg,
            force=force_modules,
        )
        log.info(
            "Module extraction done in %s | accepted=%s existing=%s duplicate=%s rejected=%s",
            _fmt_time(time.time() - t0),
            module_stats.get("accepted", 0),
            module_stats.get("skipped_existing", 0),
            module_stats.get("skipped_duplicate", 0),
            module_stats.get("rejected", 0),
        )
        _report(
            progress_callback,
            "modules",
            60,
            (
                f"Modules accepted={module_stats.get('accepted', 0)} "
                f"existing={module_stats.get('skipped_existing', 0)}"
            ),
            event="module_extraction_complete",
            modules_accepted=module_stats.get("accepted", 0),
            modules_existing=module_stats.get("skipped_existing", 0),
            modules_rejected=module_stats.get("rejected", 0),
        )

    if extract_modules_only:
        if text_model_stage_started:
            _finish_text_model_stage(cfg, active_stage="module extraction")
        return {
            "clips_created": 0,
            "clips_failed": 0,
            "moments_found": len(moments),
            "module_extraction": module_stats or {},
            "output_dir": output_dir,
        }

    modular_result = None
    if modular_requested:
        _report(progress_callback, "modular", 62, "Rendering modular assemblies...")
        log.info("\n-- MODULAR ASSEMBLY ------------------------------------------------------")
        modular_result = _run_modular_assembly(output_dir, working_dir, cfg, progress_callback)
        if modular_only:
            if text_model_stage_started:
                _finish_text_model_stage(cfg, active_stage="modular assembly")
            return {
                "clips_created": int(modular_result.get("created", 0) or 0),
                "clips_failed": int(modular_result.get("failed", 0) or 0),
                "clips_skipped": int(modular_result.get("skipped", 0) or 0),
                "clips_blocked": int(modular_result.get("blocked", 0) or 0),
                "moments_found": len(moments),
                "module_extraction": module_stats or {},
                "modular_assembly": modular_result,
                "output_dir": output_dir,
            }

    if not moments:
        log.warning("No moments detected! Check your LM Studio connection and transcript quality.")
        if text_model_stage_started:
            _finish_text_model_stage(cfg)
        return {
            "clips_created": 0,
            "clips_failed": 0,
            "moments_found": 0,
            "module_extraction": module_stats or {},
            "modular_assembly": modular_result,
            "output_dir": output_dir,
        }

    # Apply max_clips after extraction so the module library still sees the full candidate set.
    if max_clips and len(moments) > max_clips:
        log.info(f"Limiting to top {max_clips} clips (from {len(moments)} total)")
        moments = moments[:max_clips]

    _attach_detected_product_context_to_moments(moments, product_events)

    log.info(f"Moments to process: {len(moments)}")
    _report(progress_callback, "moments", 60, f"Found {len(moments)} clip moments")

    requested_variants = getattr(cfg, "VARIANTS_PER_CLIP", 1)
    selection_mode = getattr(cfg, "VARIANT_SELECTION_MODE", "fallback")
    try:
        from variation_engine import expand_moments_with_variants, resolve_variant_plan
        from variation_profile import has_active_profile

        variant_seed = getattr(cfg, "VARIANT_SEED", 42)
        _profile_variants, effective_variants, _mode = resolve_variant_plan(
            cfg,
            n_variants=requested_variants,
            selection_mode=selection_mode,
            seed=variant_seed,
        )
        if effective_variants > 1 or has_active_profile(cfg):
            log.info("\n-- VARIATION ENGINE ------------------------------------------------------")
            log.info(
                "  Base moments: %s | Variants per clip: %s | Selection: %s",
                len(moments),
                effective_variants,
                selection_mode,
            )
            moments = expand_moments_with_variants(
                moments,
                cfg,
                n_variants=requested_variants,
                seed=variant_seed,
                selection_mode=selection_mode,
            )
            log.info(f"  Total clip jobs after expansion: {len(moments)}")
    except ImportError:
        log.warning("variation_engine.py not found; skipping variations")

    # ══════════════════════════════════════════════════════════════════════════
    # STAGE 4: CUT + EDIT CLIPS
    # ══════════════════════════════════════════════════════════════════════════
    log.info("\n── STAGE 4: CUT & EDIT CLIPS ─────────────────────────────────────")

    raw_dir = Path(working_dir) / "raw_cuts"
    raw_dir.mkdir(exist_ok=True)

    manifest_path = Path(output_dir) / "manifest.json"
    render_state_path = _render_state_path_for_manifest(manifest_path)
    render_fingerprint = _render_fingerprint(video_path, cfg, max_clips, cut_only)
    render_fingerprint_extra = _render_fingerprint_extra(cfg, max_clips, cut_only)
    render_state = _load_matching_render_state(render_state_path, render_fingerprint)
    force_render_existing = False
    existing_manifest = []
    if manifest_path.exists():
        force_render_existing = not stage_fingerprint_matches(
            manifest_path,
            video_path,
            cfg,
            "ffmpeg",
            extra=render_fingerprint_extra,
        )
        if force_render_existing and render_state:
            existing_manifest = _load_manifest_rows(manifest_path)
            force_render_existing = False
            log.info("Resuming partial render from %s", render_state_path)
        elif force_render_existing:
            log.info("Existing rendered clips are stale for current render settings; they will be regenerated.")
        else:
            existing_manifest = _load_manifest_rows(manifest_path)

    jobs = [_build_clip_job(moment, i, output_dir, raw_dir) for i, moment in enumerate(moments)]
    settings_revision = str(
        getattr(cfg, "SETTINGS_REVISION", getattr(cfg, "_settings_revision", "unknown"))
        or "unknown"
    )
    for job in jobs:
        job["settings_revision"] = settings_revision
    if existing_manifest and not force_render_existing:
        reused = _reuse_existing_manifest_outputs_for_jobs(jobs, existing_manifest, Path(output_dir))
        if reused:
            log.info("  Reusing %s previously packaged/rendered output path(s)", reused)
    if force_render_existing:
        for job in jobs:
            job["force_render_existing"] = True
    _attach_precomputed_clip_contexts(jobs, transcript["words"], product_events)
    _attach_precomputed_compliance(jobs, cfg)
    max_workers = max(1, int(getattr(cfg, "MAX_PARALLEL_CLIPS", 6)))
    log.info(f"  Total jobs: {len(jobs)} | Parallel workers: {max_workers}")

    if getattr(cfg, "EDIT_LOG_CLIP_PLAN", False):
        for job in jobs:
            log.info(
                f"  [{job['index']+1:03d}/{len(jobs):03d}] {job['clip_id']} | "
                f"t={job['start']:.1f}s-{job['end']:.1f}s | score={job['score']} | "
                f"type={job['clip_type']} | product={job['product']}"
            )

    completed_rows = [] if force_render_existing else _completed_resume_rows(jobs, existing_manifest, Path(output_dir))
    manifest_rows = _manifest_rows_by_clip(completed_rows)
    completed_clip_ids = set(manifest_rows)
    pending_jobs = [job for job in jobs if str(job.get("clip_id") or "") not in completed_clip_ids]
    if completed_clip_ids:
        log.info("  Resume: %s/%s clip job(s) already complete", len(completed_clip_ids), len(jobs))

    _report(
        progress_callback,
        "editing",
        50,
        f"Rendering {len(pending_jobs)} remaining of {len(jobs)} clips...",
        event="clip_batch_start",
        clips_total=len(jobs),
        clips_completed=len(completed_clip_ids),
        clips_created=sum(
            1
            for row in completed_rows
            if str(row.get("status") or "").casefold() not in {"failed", "compliance_blocked", ""}
        ),
        clips_failed=0,
        clips_skipped=sum(1 for row in completed_rows if str(row.get("status") or "").casefold() == "skipped"),
        clips_blocked=sum(
            1 for row in completed_rows if str(row.get("status") or "").casefold() == "compliance_blocked"
        ),
        active_clip_renders=0,
        render_state_path=str(render_state_path),
    )
    try:
        manifest, render_counts = _render_clip_jobs_incremental(
            jobs,
            pending_jobs,
            manifest_rows,
            manifest_path,
            render_state_path,
            render_fingerprint,
            video_path,
            transcript["words"],
            product_events,
            cut_only,
            cfg,
            progress_callback=progress_callback,
            control_path=control_path,
        )
    except PipelinePaused:
        if text_model_stage_started:
            try:
                _finish_text_model_stage(cfg, active_stage="paused render", required=False)
            except Exception as exc:
                log.warning(f"Could not unload text model after graceful pause: {exc}")
        raise
    completed = int(render_counts.get("clips_completed", len(manifest)) or 0)
    clips_created = int(render_counts.get("clips_created", 0) or 0)
    clips_failed = int(render_counts.get("clips_failed", 0) or 0)
    clips_skipped = int(render_counts.get("clips_skipped", 0) or 0)
    clips_blocked = int(render_counts.get("clips_blocked", 0) or 0)

    # ══════════════════════════════════════════════════════════════════════════
    # DONE
    # ══════════════════════════════════════════════════════════════════════════
    clip_scores = _score_rendered_clips(jobs, manifest, output_dir, cfg, progress_callback)
    vision_scoring_requested = bool(
        clip_scores and getattr(cfg, "SCORER_VISION_ENABLED", False)
    )
    text_model_unloaded = _finish_text_model_stage_for_vision_handoff(
        cfg,
        text_model_stage_started=text_model_stage_started,
        vision_scoring_requested=vision_scoring_requested,
        active_stage="stage 5 clip scoring",
    )

    if vision_scoring_requested:
        if text_model_unloaded or not _model_management_enabled(cfg):
            clip_scores = _score_rendered_clip_host_focus(
                clip_scores,
                manifest,
                output_dir,
                cfg,
                progress_callback,
            )
        else:
            raise RuntimeError(
                "Host-focus vision scoring cannot start because "
                f"{_text_model_id(cfg)} is still loaded after text scoring."
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
        extra=render_fingerprint_extra,
    )
    _write_render_state(
        render_state_path,
        render_fingerprint,
        "complete",
        len(jobs),
        manifest,
        {
            "clips_completed": completed,
            "clips_created": clips_created,
            "clips_failed": clips_failed,
            "clips_skipped": clips_skipped,
            "clips_blocked": clips_blocked,
        },
        active_clip_renders=0,
    )
    scores_summary_path = Path(output_dir) / "scores_summary.json" if clip_scores else None
    try:
        from compliance_checker import update_scores_summary_with_compliance

        update_scores_summary_with_compliance(output_dir, manifest)
    except Exception as exc:
        log.warning(f"Could not merge compliance fields into score summary: {exc}")

    export_batch_result = _package_export_batches_if_enabled(
        cfg,
        progress_callback,
        max_clips=max_clips,
    )

    log.info("\n" + "=" * 70)
    log.info("PIPELINE COMPLETE")
    log.info(f"  Total time:     {_fmt_time(total_time)}")
    log.info(f"  Moments found:  {len(moments)}")
    log.info(f"  Clips created:  {clips_created}")
    log.info(f"  Clips failed:   {clips_failed}")
    log.info(f"  Clips skipped:  {clips_skipped} (already existed)")
    log.info(f"  Clips blocked:  {clips_blocked} (compliance)")
    log.info(f"  Clips scored:   {len(clip_scores)}")
    scorer_accounting = _read_score_optimization_stats(scores_summary_path)
    if scorer_accounting:
        vision_stats = scorer_accounting.get("vision_scoring", {})
        if not isinstance(vision_stats, dict):
            vision_stats = {}
        log.info(
            "  Qwen HTTP calls: text=%s vision=%s",
            scorer_accounting.get("actual_text_qwen_calls", 0),
            scorer_accounting.get(
                "actual_vision_qwen_calls",
                vision_stats.get("actual_vision_qwen_calls", 0),
            ),
        )
    if module_stats is not None:
        log.info(f"  Modules added:  {module_stats.get('accepted', 0)}")
    log.info(f"  Output dir:     {output_dir}")
    log.info(f"  Manifest:       {manifest_path}")
    if scores_summary_path:
        log.info(f"  Scores:         {scores_summary_path}")
    if export_batch_result:
        if export_batch_result.get("skipped"):
            log.info(
                "  Export batches: skipped (%s)",
                export_batch_result.get("reason", "not_applicable"),
            )
        elif export_batch_result.get("async"):
            log.info(
                "  Export batches: background packaging started (%s)",
                export_batch_result.get("thread_name"),
            )
        else:
            log.info(
                "  Export batches: %s moved, %s duplicate(s), root=%s",
                export_batch_result.get("packaged_count", 0),
                export_batch_result.get("duplicate_existing_count", 0)
                + export_batch_result.get("duplicate_candidate_count", 0),
                export_batch_result.get("batch_root"),
            )
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
        clips_blocked=clips_blocked,
        output_dir=output_dir,
        manifest_path=str(manifest_path),
        scores_summary_path=str(scores_summary_path) if scores_summary_path else None,
        export_batch_manifest=export_batch_result.get("manifest_path") if export_batch_result else None,
        export_batches_packaged=export_batch_result.get("packaged_count", 0) if export_batch_result else 0,
        modules_accepted=(module_stats or {}).get("accepted", 0),
    )

    return {
        "clips_created": clips_created,
        "clips_failed": clips_failed,
        "clips_skipped": clips_skipped,
        "clips_blocked": clips_blocked,
        "moments_found": len(moments),
        "total_time": total_time,
        "output_dir": output_dir,
        "manifest_path": str(manifest_path),
        "scores_summary_path": str(scores_summary_path) if scores_summary_path else None,
        "clips_scored": len(clip_scores),
        "export_batches": export_batch_result,
        "module_extraction": module_stats or {},
        "modular_assembly": modular_result,
    }


def _pipeline_service_executor(command, runtime_cfg, progress_callback):
    return _run_pipeline_impl(
        video_path=command.video_path,
        skip_transcribe=command.skip_transcribe,
        skip_moments=command.skip_moments,
        skip_vision=command.skip_vision,
        cut_only=command.cut_only,
        max_clips=command.max_clips,
        min_score=command.min_score,
        force_rescore=command.force_rescore,
        extract_modules_only=command.extract_modules_only,
        force_modules=command.force_modules,
        render_modules=command.render_modules,
        modular_only=command.modular_only,
        module_assembly_limit=command.module_assembly_limit,
        module_product_zoom=command.module_product_zoom,
        output_tag=command.output_tag,
        working_tag=command.working_tag,
        control_path=command.control_path,
        progress_callback=progress_callback,
        runtime_cfg=runtime_cfg,
        settings_overrides=command.settings_overrides,
    )


def run_pipeline(
    video_path: str,
    skip_transcribe: bool = False,
    skip_moments: bool = False,
    skip_vision: bool = False,
    cut_only: bool = False,
    max_clips: int = None,
    min_score: float = None,
    force_rescore: bool = False,
    extract_modules_only: bool = False,
    force_modules: bool = False,
    render_modules: bool = False,
    modular_only: bool = False,
    module_assembly_limit: int | None = None,
    module_product_zoom: bool = False,
    output_tag: str | None = None,
    working_tag: str | None = None,
    control_path: str | None = None,
    progress_callback=None,
    settings_overrides: dict | None = None,
):
    """Compatibility facade for the typed pipeline application service."""
    if os.environ.get("CLIPPER_SERVICE_BOUNDARY", "service").casefold() == "legacy":
        return _run_pipeline_impl(
            video_path=video_path,
            skip_transcribe=skip_transcribe,
            skip_moments=skip_moments,
            skip_vision=skip_vision,
            cut_only=cut_only,
            max_clips=max_clips,
            min_score=min_score,
            force_rescore=force_rescore,
            extract_modules_only=extract_modules_only,
            force_modules=force_modules,
            render_modules=render_modules,
            modular_only=modular_only,
            module_assembly_limit=module_assembly_limit,
            module_product_zoom=module_product_zoom,
            output_tag=output_tag,
            working_tag=working_tag,
            control_path=control_path,
            progress_callback=progress_callback,
            settings_overrides=settings_overrides,
        )

    from clipper_app.application.events import LegacyCallbackEventSink
    from clipper_app.bootstrap import build_pipeline_service
    from clipper_app.contracts import PipelineRunCommand

    command = PipelineRunCommand(
        video_path=video_path,
        skip_transcribe=skip_transcribe,
        skip_moments=skip_moments,
        skip_vision=skip_vision,
        cut_only=cut_only,
        max_clips=max_clips,
        min_score=min_score,
        force_rescore=force_rescore,
        extract_modules_only=extract_modules_only,
        force_modules=force_modules,
        render_modules=render_modules,
        modular_only=modular_only,
        module_assembly_limit=module_assembly_limit,
        module_product_zoom=module_product_zoom,
        output_tag=output_tag,
        working_tag=working_tag,
        control_path=control_path,
        settings_overrides=settings_overrides or {},
    )
    result = build_pipeline_service(_pipeline_service_executor).run(
        command,
        LegacyCallbackEventSink(progress_callback),
    )
    return result.model_dump()


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


def _model_unload_timeout(cfg) -> float:
    return max(
        1.0,
        float(
            getattr(
                cfg,
                "LM_STUDIO_MODEL_UNLOAD_TIMEOUT",
                getattr(cfg, "SCORER_VISION_TIMEOUT", 600),
            )
            or 600
        ),
    )


def _model_unload_log_interval(cfg) -> float:
    return max(1.0, float(getattr(cfg, "LM_STUDIO_MODEL_UNLOAD_LOG_INTERVAL", 30) or 30))


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
            _wait_until_model_unloaded(
                vision_id,
                cfg,
                timeout=_model_unload_timeout(cfg),
                active_stage="pipeline startup",
            )
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
            manager.unload_model(vision_id, cfg=cfg, timeout=_model_unload_timeout(cfg))
            if not _wait_until_model_unloaded(
                vision_id,
                cfg,
                timeout=_model_unload_timeout(cfg),
                active_stage="stage 1/2 text model startup",
            ):
                raise RuntimeError(
                    f"Timed out waiting for {vision_id} to unload before loading {model_id}"
                )
        loaded = manager.load_model(model_id, cfg=cfg, timeout=120)
        ready = manager.wait_until_ready(model_id, cfg=cfg, timeout=120) if loaded else False
        if not loaded or not ready:
            raise RuntimeError(f"LM Studio text model {model_id} did not become ready")
        return True
    except Exception as exc:
        raise RuntimeError(f"LM Studio text model load failed: {exc}") from exc


def _finish_text_model_stage_for_vision_handoff(
    cfg,
    text_model_stage_started: bool,
    vision_scoring_requested: bool,
    active_stage: str,
) -> bool:
    if not text_model_stage_started:
        return True
    if not vision_scoring_requested:
        model_id = _text_model_id(cfg) or "configured text model"
        log.info(
            "LM Studio text stage complete: keeping Qwen text model %s loaded "
            "because host-focus vision scoring is disabled or has no scores "
            "(stage: %s)",
            model_id,
            active_stage,
        )
        return False
    return _finish_text_model_stage(
        cfg,
        active_stage=active_stage,
        required=True,
    )


def _finish_text_model_stage(
    cfg,
    active_stage: str = "text model stage",
    required: bool = False,
) -> bool:
    manager = _model_manager_module(cfg)
    if manager is None:
        return True
    model_id = _text_model_id(cfg)
    if not model_id:
        return True
    timeout = _model_unload_timeout(cfg)
    log.info(
        "LM Studio text stage complete: unloading Qwen text model %s "
        "(unload attempted during stage: %s; timeout=%.0fs)",
        model_id,
        active_stage,
        timeout,
    )
    try:
        requested = manager.unload_model(model_id, cfg=cfg, timeout=timeout)
    except Exception as exc:
        message = (
            f"LM Studio text model unload request failed for {model_id} "
            f"during {active_stage}: {exc}"
        )
        log.error(message)
        if required:
            raise RuntimeError(message) from exc
        return False
    if not requested:
        message = (
            f"LM Studio did not accept the unload request for {model_id} "
            f"during {active_stage}"
        )
        log.error(message)
        if required:
            raise RuntimeError(message)
        return False

    unloaded = _wait_until_model_unloaded(
        model_id,
        cfg,
        timeout=timeout,
        active_stage=active_stage,
    )
    if not unloaded and required:
        raise RuntimeError(
            f"Timed out after {timeout:.0f}s waiting for {model_id} to unload "
            f"before host-focus scoring. Unload was attempted during stage: {active_stage}. "
            f"Loaded models still reported: {_loaded_model_ids_for_log(manager, cfg)}"
        )
    return unloaded


def _start_vision_model_stage(
    cfg,
    active_stage: str = "stage 6 host focus vision scoring",
) -> bool:
    if not bool(getattr(cfg, "SCORER_VISION_ENABLED", False)):
        return False
    manager = _model_manager_module(cfg)
    if manager is None:
        return True
    text_id = _text_model_id(cfg)
    if text_id and manager.is_model_loaded(text_id, cfg):
        log.info(
            "Qwen text model %s is still loaded when %s attempted to load Qwen-VL; "
            "requesting unload now.",
            text_id,
            active_stage,
        )
        _finish_text_model_stage(cfg, active_stage=active_stage, required=True)
    model_id = _vision_model_id(cfg)
    if not model_id:
        return False
    timeout = max(120.0, float(getattr(cfg, "SCORER_VISION_TIMEOUT", 120) or 120))
    log.info(
        "LM Studio vision stage: loading Qwen-VL model %s after %s unload",
        model_id,
        text_id or "text model",
    )
    try:
        loaded = manager.load_model(model_id, cfg=cfg, timeout=timeout)
        ready = manager.wait_until_ready(model_id, cfg=cfg, timeout=timeout) if loaded else False
    except Exception as exc:
        raise RuntimeError(f"LM Studio vision model load failed during {active_stage}: {exc}") from exc
    if not loaded or not ready:
        raise RuntimeError(
            f"Qwen-VL model {model_id} did not become ready within {timeout:.0f}s "
            f"after {text_id or 'the text model'} unloaded."
        )
    log.info(
        "LM Studio vision stage: Qwen-VL model %s loaded and ready after %s unloaded",
        model_id,
        text_id or "text model",
    )
    return True


def _finish_vision_model_stage(
    cfg,
    active_stage: str = "stage 6 host focus vision scoring cleanup",
) -> bool:
    manager = _model_manager_module(cfg)
    if manager is None:
        return True
    model_id = _vision_model_id(cfg)
    if not model_id:
        return True
    timeout = _model_unload_timeout(cfg)
    log.info(
        "LM Studio vision stage complete: unloading Qwen-VL model %s "
        "(unload attempted during stage: %s; timeout=%.0fs)",
        model_id,
        active_stage,
        timeout,
    )
    try:
        manager.unload_model(model_id, cfg=cfg, timeout=timeout)
    except Exception as exc:
        log.error(f"LM Studio vision model unload failed during {active_stage}: {exc}")
        return False
    return _wait_until_model_unloaded(
        model_id,
        cfg,
        timeout=timeout,
        active_stage=active_stage,
    )


def _wait_until_model_unloaded(
    model_id: str,
    cfg,
    timeout: float,
    active_stage: str = "unknown",
) -> bool:
    manager = _model_manager_module(cfg)
    if manager is None:
        return True
    wait_timeout = max(1.0, float(timeout or 120.0))
    started = time.monotonic()
    deadline = started + wait_timeout
    next_progress = started + _model_unload_log_interval(cfg)
    while time.monotonic() < deadline:
        try:
            if not manager.is_model_loaded(model_id, cfg):
                elapsed = time.monotonic() - started
                log.info(
                    "LM Studio model unloaded: %s after %.1fs "
                    "(unload attempted during stage: %s)",
                    model_id,
                    elapsed,
                    active_stage,
                )
                return True
        except Exception as exc:
            elapsed = time.monotonic() - started
            log.error(
                "Could not verify model unload for %s after %.1fs "
                "(unload attempted during stage: %s): %s",
                model_id,
                elapsed,
                active_stage,
                exc,
            )
            return False
        now = time.monotonic()
        if now >= next_progress:
            log.info(
                "Waiting for %s to unload... (%ss elapsed)",
                model_id,
                int(now - started),
            )
            while next_progress <= now:
                next_progress += _model_unload_log_interval(cfg)
        time.sleep(min(2.0, max(0.1, deadline - now)))
    elapsed = time.monotonic() - started
    log.error(
        "Timed out after %.1fs waiting for LM Studio model to unload: %s "
        "(unload attempted during stage: %s; loaded models: %s)",
        elapsed,
        model_id,
        active_stage,
        _loaded_model_ids_for_log(manager, cfg),
    )
    return False


def _loaded_model_ids_for_log(manager, cfg) -> str:
    try:
        loaded_ids = manager.loaded_model_ids(cfg)
    except Exception as exc:
        return f"unavailable ({exc})"
    return ", ".join(loaded_ids) if loaded_ids else "none"


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


def _build_versioned_stem(stem: str, tag: str | None) -> str:
    if not tag:
        return stem
    safe_tag = _safe_filename(tag)
    if not safe_tag:
        return stem
    separator = "" if safe_tag.startswith("_") else "__"
    return f"{stem}{separator}{safe_tag}"


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


def _validate_startup(
    video_path: str,
    output_dir: str,
    cfg,
    skip_moments: bool,
    skip_vision: bool,
    module_extraction_enabled: bool = False,
) -> None:
    errors = []
    if not _command_available("ffmpeg"):
        errors.append("FFmpeg is not accessible on PATH")
    if not _command_available("ffprobe"):
        errors.append("FFprobe is not accessible on PATH")
    if not _source_has_audio(video_path):
        errors.append(f"Input video has no audio stream: {video_path}")
    if module_extraction_enabled:
        try:
            import portalocker  # noqa: F401
        except ImportError:
            errors.append("portalocker is required for module extraction. Run: pip install portalocker")
    if (not skip_moments or module_extraction_enabled) and not _lm_studio_responding(cfg):
        errors.append(f"LM Studio is not responding at {getattr(cfg, 'LM_STUDIO_BASE_URL', '')}")
    if not skip_vision and not Path(getattr(cfg, "YOLO_WEIGHTS", "")).exists():
        errors.append(f"YOLO weights not found: {getattr(cfg, 'YOLO_WEIGHTS', '')}")
    if not skip_vision:
        _warn_if_yolo_cuda_unavailable(cfg)
    free_bytes = shutil.disk_usage(output_dir).free
    if free_bytes < 10 * 1024**3:
        errors.append(f"Output disk has less than 10GB free: {output_dir}")
    if errors:
        message = "Startup validation failed:\n" + "\n".join(f"  - {item}" for item in errors)
        log.error(message)
        raise RuntimeError(message)




def _warn_if_yolo_cuda_unavailable(cfg) -> None:
    requested_device = str(getattr(cfg, "YOLO_DEVICE", "cpu") or "cpu").strip().lower()
    if requested_device == "cpu":
        return
    try:
        import torch
    except Exception as exc:
        log.warning(
            "Could not import torch for YOLO CUDA startup check: %s. "
            "YOLO may fail or run on CPU.",
            exc,
        )
        return
    if not torch.cuda.is_available():
        log.warning(
            "CUDA not available for YOLO - torch version: %s, cuda: %s. "
            "YOLO will run on CPU which may crash on large videos. "
            "Install CUDA-enabled PyTorch.",
            getattr(torch, "__version__", "unknown"),
            getattr(torch.version, "cuda", None),
        )


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
        description="PROYA Livestream to TikTok Clips Pipeline",
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
        "--extract-modules-only",
        action="store_true",
        help="Run transcription, moment detection, and module extraction only",
    )
    parser.add_argument(
        "--force-modules",
        action="store_true",
        help="Recut deterministic module outputs even when an existing valid module is present",
    )
    parser.add_argument(
        "--render-modules",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--assemble-modules",
        action="store_true",
        help="Assemble clips from D:\\proya_modules into a dated modular_assembly output folder without --video",
    )
    parser.add_argument(
        "--modular-only",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--module-assembly-limit",
        type=int,
        default=None,
        help="Override the modular assembly render limit for this run",
    )
    parser.add_argument(
        "--module-product-zoom",
        action="store_true",
        help="Use visually validated module product events for modular product zooms in this run",
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        metavar="YYYY-MM-DD",
        help="Limit --assemble-modules to same-date module combinations and use that output date folder",
    )
    parser.add_argument(
        "--product",
        type=str,
        default=None,
        help="Limit --assemble-modules to one product, such as serum",
    )
    parser.add_argument(
        "--validate-modules-visual-only",
        action="store_true",
        help="Run YOLO visual validation on existing module library entries without rendering",
    )
    parser.add_argument(
        "--module-visual-product",
        type=str,
        default=None,
        help="Limit --validate-modules-visual-only to one canonical product",
    )
    parser.add_argument(
        "--module-visual-status",
        choices=["not_run", "failed", "passed", "all"],
        default="not_run",
        help="Limit --validate-modules-visual-only by current visual status",
    )
    parser.add_argument(
        "--module-visual-role",
        choices=["hook", "main", "cta"],
        default=None,
        help="Limit --validate-modules-visual-only by module role",
    )
    parser.add_argument(
        "--module-visual-approved-only",
        action="store_true",
        help="Limit --validate-modules-visual-only to approved modules",
    )
    parser.add_argument(
        "--module-visual-priority",
        choices=["assembly_ready", "index_order"],
        default="assembly_ready",
        help="Order visual validation candidates by assembly priority or raw index order",
    )
    parser.add_argument(
        "--module-visual-limit",
        type=int,
        default=None,
        help="Maximum modules to visually validate in this run",
    )
    parser.add_argument(
        "--force-module-visual",
        action="store_true",
        help="Re-run module visual validation even when the stored fingerprint is current",
    )
    parser.add_argument(
        "--module-library-report",
        action="store_true",
        help="Write modular library health reports without processing a video",
    )
    parser.add_argument(
        "--module-review-queue",
        action="store_true",
        help="Write a modular review queue JSON/CSV without processing a video",
    )
    parser.add_argument(
        "--module-review-filter",
        choices=["needs_review", "approved", "blocked", "no_visual_events", "all"],
        default="needs_review",
        help="Filter used with --module-review-queue",
    )
    parser.add_argument(
        "--module-review-limit",
        type=int,
        default=None,
        help="Maximum rows to write with --module-review-queue",
    )
    parser.add_argument(
        "--module-review-set",
        type=str,
        default=None,
        metavar="MODULE_ID_OR_PATH",
        help="Approve/block a module by module_id, media path, or sidecar path",
    )
    parser.add_argument(
        "--module-review-status",
        choices=["approved", "needs_review", "blocked"],
        default=None,
        help="New review status for --module-review-set",
    )
    parser.add_argument(
        "--module-review-note",
        type=str,
        default="",
        help="Optional note stored in the reviewed module sidecar",
    )
    parser.add_argument(
        "--module-reviewer",
        type=str,
        default="operator",
        help="Reviewer name stored in the reviewed module sidecar",
    )
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
    parser.add_argument(
        "--package-export-batches",
        action="store_true",
        help="Move export-ready clips into numbered affiliate batch folders without processing a video",
    )
    parser.add_argument(
        "--cleanup-stale-queue",
        action="store_true",
        help="Reset stale queued/running queue stage markers using QUEUE_STUCK_THRESHOLD",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="Output root to package with --package-export-batches; defaults to config.OUTPUT_DIR",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=None,
        help="Maximum videos per affiliate batch folder for --package-export-batches",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview --package-export-batches without moving files",
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

    if args.render_modules or args.modular_only:
        args.render_modules = True

    if (args.product or args.date) and not args.assemble_modules:
        print("Error: --product and --date are only valid with --assemble-modules")
        sys.exit(1)

    if (
        args.module_assembly_limit is not None
        or args.module_product_zoom
    ) and not (args.assemble_modules or args.render_modules or args.modular_only):
        print("Error: module assembly options require --assemble-modules, --render-modules, or --modular-only")
        sys.exit(1)

    if args.package_export_batches:
        import config as cfg
        from export_packager import package_export_batches

        output_root = Path(args.output_dir or getattr(cfg, "OUTPUT_DIR", r"D:\output_clips"))
        result = package_export_batches(
            output_root,
            cfg=cfg,
            batch_size=args.batch_size,
            dry_run=args.dry_run,
        )
        log.info("Export batch packaging complete:")
        log.info("  Output root: %s", result.get("output_root"))
        log.info("  Batch root:  %s", result.get("batch_root"))
        log.info("  Manifest:    %s", result.get("manifest_path"))
        log.info("  Legacy cutoff: %s", result.get("legacy_batch_folder_cutoff"))
        log.info("  Eligible:    %s", result.get("eligible_count", 0))
        log.info("  New unique:  %s", result.get("new_unique_count", 0))
        log.info("  Packaged:    %s", result.get("packaged_count", 0))
        log.info(
            "  Duplicates:  existing=%s candidate=%s",
            result.get("duplicate_existing_count", 0),
            result.get("duplicate_candidate_count", 0),
        )
        if result.get("dry_run"):
            log.info("  Dry run only; no files moved.")
        if result.get("errors"):
            for error in result.get("errors", [])[:10]:
                log.warning("  %s", error)
        return

    if args.cleanup_stale_queue:
        from video_queue import cleanup_stale_queue_state

        result = cleanup_stale_queue_state()
        log.info("Stale queue cleanup complete:")
        log.info("  State:   %s", result.get("state_path"))
        log.info("  Exists:  %s", result.get("exists"))
        log.info("  Changed: %s", result.get("changed", 0))
        return

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

    if args.module_library_report:
        from clipper_app.bootstrap import build_module_service
        from clipper_app.contracts import ModuleReportCommand

        report = build_module_service().report(
            ModuleReportCommand(include_library_report=True, include_review_queue=False)
        ).payload["report"]
        log.info("Module library report written:")
        log.info("  JSON: %s", report.get("json_path"))
        log.info("  CSV:  %s", report.get("csv_path"))
        for row in report.get("readiness", []):
            log.info(
                "  %-11s ready=%s hook=%s main=%s cta=%s sources=%s reason=%s",
                row.get("product"),
                row.get("ready"),
                row.get("approved_hook"),
                row.get("approved_main"),
                row.get("approved_cta"),
                row.get("source_video_count"),
                row.get("reason"),
            )
        return

    if args.module_review_queue:
        from clipper_app.bootstrap import build_module_service
        from clipper_app.contracts import ModuleReportCommand

        report = build_module_service().report(
            ModuleReportCommand(
                include_library_report=False,
                include_review_queue=True,
                review_filter=args.module_review_filter,
                review_limit=args.module_review_limit,
            )
        ).payload["review_queue"]
        log.info("Module review queue written:")
        log.info("  JSON: %s", report.get("json_path"))
        log.info("  CSV:  %s", report.get("csv_path"))
        log.info("  Rows: %s", report.get("module_count", 0))
        for row in report.get("counts_by_quality_status", []):
            log.info("  %-13s %s", row.get("quality_status"), row.get("count"))
        return

    if args.module_review_set:
        if not args.module_review_status:
            print("Error: --module-review-set requires --module-review-status")
            sys.exit(1)
        from clipper_app.bootstrap import build_module_service
        from clipper_app.contracts import ModuleReviewCommand

        result = build_module_service().review(ModuleReviewCommand(
            identifier=args.module_review_set,
            status=args.module_review_status,
            note=args.module_review_note,
            reviewer=args.module_reviewer,
        )).payload
        log.info(
            "Module review updated: %s review_status=%s quality_status=%s reason=%s",
            result.get("module_id"),
            result.get("review_status"),
            result.get("quality_status"),
            result.get("quality_reason"),
        )
        log.info("  Sidecar: %s", result.get("sidecar_path"))
        return

    if args.validate_modules_visual_only:
        from clipper_app.bootstrap import build_module_service
        from clipper_app.contracts import ModuleValidationCommand

        result = build_module_service().validate(ModuleValidationCommand(
            product=args.module_visual_product,
            limit=args.module_visual_limit,
            visual_status=args.module_visual_status,
            role=args.module_visual_role,
            approved_only=args.module_visual_approved_only,
            priority=args.module_visual_priority,
            force=args.force_module_visual,
        )).payload
        log.info("Module visual validation complete:")
        log.info("  Index:   %s", result.get("index_path"))
        log.info("  Checked: %s", result.get("validated", 0))
        log.info("  Passed:  %s", result.get("passed", 0))
        log.info("  Failed:  %s", result.get("failed", 0))
        log.info("  Not run: %s", result.get("not_run", 0))
        log.info("  Current: %s", result.get("skipped_current", 0))
        log.info("  Filtered: %s", result.get("skipped_filter", 0))
        log.info("  Errors:  %s", result.get("sidecar_error", 0))
        return

    if args.assemble_modules:
        try:
            from clipper_app.bootstrap import build_module_service
            from clipper_app.contracts import ModuleAssemblyCommand

            build_module_service().assemble(ModuleAssemblyCommand(
                assembly_date=args.date,
                product=args.product,
                module_assembly_limit=args.module_assembly_limit,
                module_product_zoom=args.module_product_zoom,
            ))
        except ValueError as exc:
            print(f"Error: {exc}")
            sys.exit(1)
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
        print("\nError: --video is required unless using --train-yolo, --test-lm-studio, --assemble-modules, --module-library-report, --module-review-queue, --module-review-set, --validate-modules-visual-only, --cleanup-stale-queue, --preview-ba, or --setup-sfx")
        print("       Use --package-export-batches to package existing export-ready clips without --video.")
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
        extract_modules_only=args.extract_modules_only,
        force_modules=args.force_modules,
        render_modules=args.render_modules,
        modular_only=args.modular_only,
        module_assembly_limit=args.module_assembly_limit,
        module_product_zoom=args.module_product_zoom,
        output_tag=output_tag,
        working_tag=working_tag,
    )


def _test_lm_studio():
    """Quick test to verify LM Studio is running and responding."""
    import config as cfg

    log.info(f"Testing LM Studio at {cfg.LM_STUDIO_BASE_URL}...")
    try:
        from openai import OpenAI
        from utils import lm_studio_openai_chat_kwargs
        client = OpenAI(base_url=cfg.LM_STUDIO_BASE_URL, api_key=cfg.LM_STUDIO_API_KEY)

        model_id = cfg.LM_STUDIO_MODEL
        response = client.chat.completions.create(
            model=model_id,
            messages=[
                {"role": "user", "content": "Balas hanya dengan 'OK' jika kamu bisa mendengar saya."}
            ],
            max_tokens=10,
            timeout=30,
            **lm_studio_openai_chat_kwargs(cfg, model_id=model_id),
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
