#!/usr/bin/env python3
from __future__ import annotations

import base64
import copy
import json
import logging
import math
import re
import shutil
import subprocess
from collections import Counter
from datetime import datetime, timezone
from bisect import bisect_left, bisect_right
from pathlib import Path
from typing import Any

from utils import _parse_json_object

log = logging.getLogger("proya.clip_scorer")

SCORE_SCHEMA_VERSION = 2
SCORE_TIER_DIRS = {"export_ready", "review_needed", "rejected"}


def score_clip(
    rendered_clip_path: str | Path,
    transcript: str | Path | dict | list,
    product_name: str,
    cfg=None,
    frame_sample_rate: int | None = None,
    clip_id: str | None = None,
    output_file: str | None = None,
) -> dict[str, Any]:
    """Score one rendered clip and return a JSON-serializable score object."""
    if cfg is None:
        import config as cfg  # type: ignore

    clip_path = Path(rendered_clip_path)
    sample_rate = max(
        1,
        int(frame_sample_rate or getattr(cfg, "SCORER_FRAME_SAMPLE_RATE", 10) or 10),
    )
    vision_sample_rate = max(
        1,
        int(getattr(cfg, "SCORER_VISION_FRAME_SAMPLE_RATE", 150) or 150),
    )
    weights = _normalized_dimension_weights(getattr(cfg, "SCORER_WEIGHTS", None))
    cached_score = _load_valid_cached_score_for_clip(
        clip_path,
        clip_id or clip_path.stem,
        output_file or clip_path.name,
        cfg,
        weights,
    )
    if cached_score is not None:
        log.info("Score cache hit for %s", clip_path)
        return cached_score

    transcript_text = transcript_to_text(transcript)
    flags: list[str] = []
    metrics: dict[str, Any] = {}

    content_score = None
    content_summary = ""
    engagement_score = 0.0
    try:
        combined = _score_content_and_engagement_with_qwen(transcript_text, product_name, cfg)
        content = combined["content"]
        content_score = content["score"]
        flags.extend(content["flags"])
        content_summary = content["summary"]
        metrics["content"] = content.get("metrics", {})
        engagement = combined["engagement"]
        engagement_score = engagement["score"]
        flags.extend(engagement["flags"])
        metrics["engagement"] = engagement.get("metrics", {})
    except Exception as exc:
        log.warning("Content/engagement Qwen scoring unavailable for %s: %s", clip_path, exc)
        flags.append("content_qwen_unavailable")
        content_summary = ""
        metrics["content"] = {"error": str(exc)}
        engagement_score, engagement_flags, engagement_metrics = _score_engagement(
            transcript_text,
            product_name,
            cfg,
        )
        flags.extend(engagement_flags)
        metrics["engagement"] = {
            **engagement_metrics,
            "source": "keyword_fallback",
            "fallback_reason": str(exc),
        }

    hook_score = None
    hook_summary = ""
    try:
        hook = _score_hook_with_qwen(transcript, product_name, cfg)
        hook_score = hook.get("score")
        hook_summary = str(hook.get("summary") or "")
        metrics["hook"] = hook.get("metrics", {})
    except Exception as exc:
        log.warning("Hook scoring unavailable for %s: %s", clip_path, exc)
        metrics["hook"] = {"error": str(exc), "source": "unavailable"}

    host_focus_score = None
    metrics["host_focus"] = {
        "enabled": False,
        "reason": "deferred_to_vision_scoring_stage",
        "frame_sample_rate": vision_sample_rate,
    }

    try:
        quality_score, quality_flags, quality_metrics = _score_quality(clip_path, cfg)
        flags.extend(quality_flags)
        metrics["quality"] = quality_metrics
    except Exception as exc:
        log.warning("Quality scoring unavailable for %s: %s", clip_path, exc)
        quality_score = None
        flags.append("quality_probe_unavailable")
        metrics["quality"] = {"error": str(exc)}

    dimension_scores = {
        "content": content_score,
        "quality": quality_score,
        "engagement": engagement_score,
    }
    weights = _effective_dimension_weights(cfg, host_focus_score)
    if "host_focus" in weights:
        dimension_scores["host_focus"] = host_focus_score
    total_score = _weighted_total(dimension_scores, weights)

    summary = content_summary or _fallback_summary(dimension_scores, flags)
    final_flags = _dedupe_flags(flags)
    if not transcript_text.strip():
        final_flags = _dedupe_flags(["no_transcript"] + final_flags)

    total_score, score_caps_applied = _apply_score_caps(
        total_score,
        final_flags,
        host_focus_score,
        cfg,
    )
    export_threshold = float(getattr(cfg, "SCORER_MIN_SCORE_TO_EXPORT", 0.0) or 0.0)
    exported = export_threshold <= 0.0 or total_score >= export_threshold
    if not exported:
        final_flags = _dedupe_flags(final_flags + ["below_export_threshold"])

    return {
        "schema_version": SCORE_SCHEMA_VERSION,
        "clip_id": clip_id or clip_path.stem,
        "output_file": output_file or clip_path.name,
        "clip_path": str(clip_path.resolve()),
        "product": product_name or "general",
        "content_score": _round_optional_score(content_score),
        "visual_score": None,
        "host_focus_score": _round_optional_score(host_focus_score),
        "hook_score": _round_optional_score(hook_score),
        "hook_summary": hook_summary,
        "quality_score": _round_optional_score(quality_score),
        "engagement_score": _round_optional_score(engagement_score),
        "total_score": _round_score(total_score),
        "weights": weights,
        "scored_dimensions": {
            **{key: value is not None for key, value in dimension_scores.items()},
            "host_focus": host_focus_score is not None,
            "hook": hook_score is not None,
        },
        "flags": final_flags,
        "score_caps_applied": score_caps_applied,
        "summary": summary,
        "metrics": metrics,
        "frame_sample_rate": sample_rate,
        "vision_frame_sample_rate": vision_sample_rate,
        "export_threshold": export_threshold,
        "exported": exported,
        "clip_mtime_ns": _clip_mtime_ns(clip_path),
        "scored_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
    }


def transcript_to_text(transcript: str | Path | dict | list | None) -> str:
    """Accept pipeline transcript JSON, clip words, plain text, or a file path."""
    if transcript is None:
        return ""

    if isinstance(transcript, Path):
        return _read_transcript_path(transcript)

    if isinstance(transcript, str):
        try:
            candidate = Path(transcript)
            if len(transcript) < 260 and candidate.exists():
                return _read_transcript_path(candidate)
        except (OSError, ValueError):
            pass
        return " ".join(transcript.split())

    if isinstance(transcript, dict):
        if isinstance(transcript.get("text"), str):
            return " ".join(transcript["text"].split())
        if isinstance(transcript.get("words"), list):
            return _words_to_text(transcript["words"])
        if isinstance(transcript.get("segments"), list):
            return " ".join(
                str(segment.get("text", "")).strip()
                for segment in transcript["segments"]
                if isinstance(segment, dict)
            ).strip()
        return ""

    if isinstance(transcript, list):
        if all(isinstance(item, str) for item in transcript):
            return " ".join(str(item).strip() for item in transcript).strip()
        return _words_to_text(transcript)

    return ""


def write_score_artifacts(
    scores: list[dict[str, Any]],
    output_dir: str | Path,
    groups: list[dict[str, Any]] | None = None,
    optimization_stats: dict[str, Any] | None = None,
    cfg=None,
    finalize: bool = True,
) -> dict[str, Any]:
    """Write per-folder scores.json files and a ranked grouped scores_summary.json."""
    if cfg is None:
        try:
            import config as cfg  # type: ignore
        except Exception:
            cfg = None

    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    valid_scores = [_public_score(score) for score in scores if isinstance(score, dict)]
    valid_groups = (
        [_public_group(group) for group in groups if isinstance(group, dict)]
        if groups is not None
        else _build_score_groups_from_flat_scores(valid_scores)
    )
    post_scoring = {}
    report_path = ""
    if finalize:
        try:
            tier_move = _move_scored_clips_by_tier(valid_scores, root, cfg)
            _apply_tier_moves_to_groups(valid_groups, tier_move, root)
            post_scoring["tier_move"] = tier_move
        except Exception as exc:
            log.warning("Could not move scored clips into tiers for %s: %s", root, exc)

    by_parent: dict[Path, list[dict[str, Any]]] = {}
    for score in valid_scores:
        clip_path = Path(str(score.get("clip_path", "")))
        parent = clip_path.parent if clip_path.parent != Path("") else root
        by_parent.setdefault(parent, []).append(score)

    local_files = []
    for parent, parent_scores in by_parent.items():
        parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": SCORE_SCHEMA_VERSION,
            "updated_at": now,
            "clips": sorted(parent_scores, key=lambda item: str(item.get("clip_id", ""))),
            "groups": _build_score_groups_from_flat_scores(parent_scores),
        }
        path = parent / "scores.json"
        _write_json_atomic(path, payload)
        local_files.append(str(path.resolve()))

    ranked = sorted(
        valid_scores,
        key=lambda item: _safe_float(item.get("total_score"), default=-1.0),
        reverse=True,
    )
    ranked_groups = sorted(
        valid_groups,
        key=lambda item: _safe_float(item.get("total_score"), default=-1.0),
        reverse=True,
    )
    summary_path = root / "scores_summary.json"
    summary_payload = {
        "schema_version": SCORE_SCHEMA_VERSION,
        "updated_at": now,
        "output_dir": str(root.resolve()),
        "score_count": len(ranked),
        "base_score_count": len(ranked_groups),
        "variant_score_count": len(ranked),
        "scoring_optimization": optimization_stats or _build_scoring_optimization_stats(ranked, ranked_groups),
        "groups": ranked_groups,
        "clips": ranked,
    }
    if finalize:
        try:
            report_path = str(write_scores_report(ranked, root, cfg).resolve())
            post_scoring["scores_report_path"] = report_path
        except Exception as exc:
            log.warning("Could not write score trend report for %s: %s", root, exc)

        if post_scoring:
            summary_payload["post_scoring"] = post_scoring
            if report_path:
                summary_payload["scores_report_path"] = report_path

    _write_json_atomic(summary_path, summary_payload)

    return {
        "summary_path": str(summary_path.resolve()),
        "local_score_files": local_files,
        "scores_report_path": report_path,
        "tier_move": post_scoring.get("tier_move", {}),
    }


def score_clip_variants(
    entries: list[dict[str, Any]],
    cfg=None,
    print_progress: bool = False,
    on_group_scored: Any | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Score base clips once, inherit scores to variants, and compute variant similarity."""
    if cfg is None:
        import config as cfg  # type: ignore

    normalized_entries = [
        entry
        for entry in (_normalize_score_entry(item) for item in entries)
        if entry is not None
    ]
    if not normalized_entries:
        return [], [], _build_scoring_optimization_stats([], [])

    grouped_entries = _group_score_entries_by_base_clip(normalized_entries)
    previous_calls = len(normalized_entries)
    actual_calls = 0
    cache_hits = 0
    cache_misses = 0
    fresh_scores = 0
    flat_scores: list[dict[str, Any]] = []
    groups: list[dict[str, Any]] = []
    current_weights = _normalized_dimension_weights(getattr(cfg, "SCORER_WEIGHTS", None))

    for group_index, (base_clip_id, group_entries) in enumerate(grouped_entries.items(), start=1):
        representative = _select_base_score_entry(group_entries)
        cached_by_key: dict[str, dict[str, Any]] = {}
        for entry in group_entries:
            cached_score = _load_valid_cached_score_for_entry(entry, cfg, current_weights)
            if cached_score is None:
                cache_misses += 1
            else:
                cached_by_key[entry["entry_key"]] = cached_score
                cache_hits += 1

        uncached_entries = [
            entry
            for entry in group_entries
            if entry["entry_key"] not in cached_by_key
        ]
        cached_base_source = (
            cached_by_key.get(representative["entry_key"])
            or next(iter(cached_by_key.values()), None)
        )
        if cached_base_source is not None:
            base_score = _base_score_from_cached_score(cached_base_source)
        else:
            actual_calls += 1
            try:
                base_score = score_clip(
                    representative["clip_path"],
                    representative.get("transcript", ""),
                    representative.get("product", "general"),
                    cfg=cfg,
                    clip_id=base_clip_id,
                    output_file=representative.get("output_file"),
                )
            except Exception as exc:
                log.warning("Base scoring failed for %s: %s", base_clip_id, exc)
                base_score = _failed_base_score(base_clip_id, representative, exc, cfg)

        _decorate_base_score(base_score, base_clip_id, representative, group_entries)
        similarity_by_key = (
            _score_similarity_for_group(group_entries, cfg)
            if uncached_entries
            else {}
        )
        variant_scores = []

        for entry in group_entries:
            cached_score = cached_by_key.get(entry["entry_key"])
            if cached_score is not None:
                variant_score = _prepare_cached_variant_score(cached_score, entry)
            else:
                similarity = similarity_by_key.get(entry["entry_key"], {})
                variant_score = _inherit_base_score_for_variant(base_score, entry, similarity)
                variant_score["cache_hit"] = False
                fresh_scores += 1
            variant_scores.append(variant_score)
            flat_scores.append(variant_score)

        _apply_top_variant_filter_to_variant_scores(variant_scores, cfg)
        group_record = _build_group_score_from_base(base_score, variant_scores)
        groups.append(group_record)
        partial_stats = {
            "previous_scoring_calls": previous_calls,
            "actual_scoring_calls": actual_calls,
            "saved_scoring_calls": max(0, len(flat_scores) - actual_calls),
            "base_clip_count": group_index,
            "variant_clip_count": len(flat_scores),
            "cached_score_count": cache_hits,
            "fresh_score_count": fresh_scores,
            "cache_miss_count": cache_misses,
            "score_cache_enabled": bool(getattr(cfg, "SCORER_CACHE_ENABLED", True)),
            "force_rescore": bool(getattr(cfg, "SCORER_FORCE_RESCORE", False)),
            "previous_text_qwen_calls": group_index * 2,
            "actual_text_qwen_calls": actual_calls,
            "saved_text_qwen_calls": max(0, group_index * 2 - actual_calls),
            "saved_text_qwen_calls_from_merge": actual_calls,
        }

        if print_progress:
            avg_similarity = group_record.get("average_similarity_score")
            print(
                f"[{group_index}] {base_clip_id} "
                f"base_total={base_score.get('total_score')} "
                f"avg_similarity={_format_optional_score(avg_similarity)} "
                f"variants={len(group_entries)} "
                f"cache_hits={len(cached_by_key)} "
                f"fresh={len(uncached_entries)}"
            )
            for variant in group_record.get("variants", []):
                print(
                    f"    - {variant.get('clip_id')} "
                    f"similarity={_format_optional_score(variant.get('similarity_score'))}"
                )
        if on_group_scored is not None:
            on_group_scored(group_record, variant_scores, partial_stats)

    stats = {
        "previous_scoring_calls": previous_calls,
        "actual_scoring_calls": actual_calls,
        "saved_scoring_calls": max(0, previous_calls - actual_calls),
        "base_clip_count": len(grouped_entries),
        "variant_clip_count": previous_calls,
        "cached_score_count": cache_hits,
        "fresh_score_count": fresh_scores,
        "cache_miss_count": cache_misses,
        "score_cache_enabled": bool(getattr(cfg, "SCORER_CACHE_ENABLED", True)),
        "force_rescore": bool(getattr(cfg, "SCORER_FORCE_RESCORE", False)),
        "previous_text_qwen_calls": len(grouped_entries) * 2,
        "actual_text_qwen_calls": actual_calls,
        "saved_text_qwen_calls": max(0, len(grouped_entries) * 2 - actual_calls),
        "saved_text_qwen_calls_from_merge": actual_calls,
    }
    message = (
        "Grouped clip scoring saved %s full scoring call(s): previous=%s actual=%s "
        "base_clips=%s variants=%s cache_hits=%s fresh_scores=%s"
    )
    log.info(
        message,
        stats["saved_scoring_calls"],
        previous_calls,
        actual_calls,
        stats["base_clip_count"],
        stats["variant_clip_count"],
        cache_hits,
        fresh_scores,
    )
    log.info(
        "Score cache served %s clip(s); freshly scored %s clip(s); cache misses %s",
        cache_hits,
        fresh_scores,
        cache_misses,
    )
    log.info(
        "Merged content+engagement Qwen calls saved %s text call(s): previous=%s actual=%s",
        stats["saved_text_qwen_calls"],
        stats["previous_text_qwen_calls"],
        stats["actual_text_qwen_calls"],
    )
    if print_progress:
        print(
            "Grouped scoring saved "
            f"{stats['saved_scoring_calls']} full scoring call(s) "
            f"({stats['actual_scoring_calls']} base calls for {stats['variant_clip_count']} variants)."
        )
        print(
            "Score cache: "
            f"{stats['cached_score_count']} cached, {stats['fresh_score_count']} fresh, "
            f"{stats['cache_miss_count']} miss."
        )
        print(
            "Qwen text calls saved: "
            f"{stats['saved_text_qwen_calls']} "
            f"(previous {stats['previous_text_qwen_calls']}, actual {stats['actual_text_qwen_calls']})."
        )
    return flat_scores, groups, stats


def apply_host_focus_vision_scores(
    scores: list[dict[str, Any]],
    groups: list[dict[str, Any]] | None = None,
    cfg=None,
    print_progress: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Run the deferred Qwen-VL host-focus pass and recalculate totals."""
    if cfg is None:
        import config as cfg  # type: ignore

    updated_scores = [copy.deepcopy(score) for score in scores if isinstance(score, dict)]
    if not bool(getattr(cfg, "SCORER_VISION_ENABLED", False)):
        return updated_scores, groups or _build_score_groups_from_flat_scores(updated_scores), {
            "vision_scoring_enabled": False,
            "vision_scored_groups": 0,
        }

    score_groups = (
        [copy.deepcopy(group) for group in groups if isinstance(group, dict)]
        if groups is not None
        else _build_score_groups_from_flat_scores(updated_scores)
    )
    by_base: dict[str, list[dict[str, Any]]] = {}
    for score in updated_scores:
        base_clip_id = str(score.get("base_clip_id") or _base_clip_id_from_clip_id(str(score.get("clip_id") or "")))
        by_base.setdefault(base_clip_id, []).append(score)

    vision_sample_rate = max(1, int(getattr(cfg, "SCORER_VISION_FRAME_SAMPLE_RATE", 150) or 150))
    updated_groups: list[dict[str, Any]] = []
    attempted = 0
    succeeded = 0
    failed = 0
    skipped = 0

    for group_index, group in enumerate(score_groups, start=1):
        base_clip_id = str(group.get("base_clip_id") or group.get("clip_id") or "")
        group_scores = by_base.get(base_clip_id, [])
        representative_path = _representative_clip_path_for_host_focus(group, group_scores)
        if representative_path is None:
            skipped += 1
            host_focus = {
                "score": None,
                "flags": ["host_focus_no_clip"],
                "metrics": {
                    "enabled": True,
                    "error": "representative clip missing",
                    "frame_sample_rate": vision_sample_rate,
                },
            }
        else:
            attempted += 1
            try:
                host_focus = _score_host_focus_with_qwen_vl(
                    representative_path,
                    cfg,
                    vision_sample_rate,
                    clip_id=base_clip_id or representative_path.stem,
                )
                succeeded += 1
            except Exception as exc:
                failed += 1
                message = f"Host focus vision scoring failed for {representative_path}: {exc}"
                log.error(message)
                raise RuntimeError(message) from exc

        base_score = copy.deepcopy(group)
        _apply_host_focus_result_to_score(base_score, host_focus, cfg)
        for score in group_scores:
            _apply_host_focus_result_to_score(score, host_focus, cfg)
        _apply_top_variant_filter_to_variant_scores(group_scores, cfg)
        group_record = (
            _build_group_score_from_base(base_score, group_scores)
            if group_scores
            else base_score
        )
        updated_groups.append(group_record)
        if print_progress:
            print(
                f"[vision {group_index}/{len(score_groups)}] {base_clip_id} "
                f"host_focus={_format_optional_score(host_focus.get('score'))} "
                f"total={_format_optional_score(group_record.get('total_score'))}"
            )

    stats = {
        "vision_scoring_enabled": True,
        "vision_base_group_count": len(score_groups),
        "vision_attempted_groups": attempted,
        "vision_scored_groups": succeeded,
        "vision_failed_groups": failed,
        "vision_skipped_groups": skipped,
        "vision_model": getattr(cfg, "SCORER_VISION_MODEL", getattr(cfg, "SCORER_VISION_MODEL_ID", "")),
        "vision_frame_sample_rate": vision_sample_rate,
    }
    log.info(
        "Host-focus vision scoring complete: attempted=%s scored=%s failed=%s skipped=%s",
        attempted,
        succeeded,
        failed,
        skipped,
    )
    return updated_scores, updated_groups, stats


def _representative_clip_path_for_host_focus(
    group: dict[str, Any],
    group_scores: list[dict[str, Any]],
) -> Path | None:
    candidates = [
        group.get("representative_clip_path"),
        group.get("clip_path"),
    ]
    if group_scores:
        representative = _select_representative_score(group_scores)
        candidates.extend([representative.get("clip_path"), representative.get("representative_clip_path")])
    for value in candidates:
        if not value:
            continue
        path = Path(str(value))
        if path.exists():
            return path
    return None


def _apply_host_focus_result_to_score(score: dict[str, Any], host_focus: dict[str, Any], cfg) -> None:
    host_focus_score = _round_optional_score(host_focus.get("score"))
    host_flags = _dedupe_flags(list(host_focus.get("flags", [])))
    flags = [
        flag
        for flag in list(score.get("flags", []))
        if not _is_host_focus_flag(flag) and _clean_flag(flag) != "below_export_threshold"
    ]
    flags.extend(host_flags)
    if host_focus_score is not None and _safe_float(host_focus_score, default=10.0) < 4.0:
        flags.append("host_not_focused")
    final_flags = _dedupe_flags(flags)

    metrics = score.get("metrics", {})
    if not isinstance(metrics, dict):
        metrics = {}
    metrics["host_focus"] = host_focus.get("metrics", {})
    score["metrics"] = metrics
    score["host_focus_score"] = host_focus_score

    dimension_scores = {
        "content": score.get("content_score"),
        "quality": score.get("quality_score"),
        "engagement": score.get("engagement_score"),
    }
    weights = _effective_dimension_weights(cfg, host_focus_score)
    if "host_focus" in weights:
        dimension_scores["host_focus"] = host_focus_score
    total_score = _weighted_total(dimension_scores, weights)
    total_score, score_caps_applied = _apply_score_caps(
        total_score,
        final_flags,
        host_focus_score,
        cfg,
    )

    export_threshold = float(getattr(cfg, "SCORER_MIN_SCORE_TO_EXPORT", 0.0) or 0.0)
    exported = export_threshold <= 0.0 or total_score >= export_threshold
    if not exported:
        final_flags = _dedupe_flags(final_flags + ["below_export_threshold"])

    score["weights"] = weights
    score["total_score"] = _round_score(total_score)
    score["score_caps_applied"] = score_caps_applied
    score["flags"] = final_flags
    score["export_threshold"] = export_threshold
    score["exported"] = exported
    score["scored_dimensions"] = {
        "content": score.get("content_score") is not None,
        "quality": score.get("quality_score") is not None,
        "engagement": score.get("engagement_score") is not None,
        "host_focus": host_focus_score is not None,
        "hook": score.get("hook_score") is not None,
    }
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    score["vision_scored_at"] = now
    score["scored_at"] = now


def _is_host_focus_flag(flag: Any) -> bool:
    clean = _clean_flag(flag)
    return clean.startswith("host_") or clean in {"host_focus_vision_unavailable"}


def score_output_folder(
    output_dir: str | Path,
    working_dir: str | Path | None = None,
    cfg=None,
    limit: int | None = None,
    include_failed: bool = False,
    flush_every: int | None = None,
) -> list[dict[str, Any]]:
    """Score every rendered clip listed in one output folder's manifest."""
    if cfg is None:
        import config as cfg  # type: ignore

    folder = Path(output_dir)
    manifest_path = folder / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"No manifest.json found in {folder}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, list):
        raise ValueError(f"Expected manifest.json to be a list: {manifest_path}")

    resolved_working_dir = Path(working_dir) if working_dir else _infer_working_dir(folder, cfg)
    transcript_words = _load_transcript_words(resolved_working_dir)
    word_index = _build_transcript_word_index(transcript_words)

    entries: list[dict[str, Any]] = []
    for row in manifest:
        if not isinstance(row, dict):
            continue
        status = str(row.get("status") or "")
        if status == "failed" and not include_failed:
            continue
        clip_path = folder / str(row.get("output_file", "")).replace("\\", "/")
        if not clip_path.exists():
            continue

        transcript_input = _get_clip_words_from_index(
            word_index,
            row.get("start"),
            row.get("end"),
        )
        entries.append(
            {
                "clip_path": clip_path,
                "transcript": transcript_input,
                "product": str(row.get("product") or "general"),
                "clip_id": str(row.get("clip_id") or clip_path.stem),
                "output_file": str(row.get("output_file") or clip_path.name),
                "version_dir": row.get("version_dir", ""),
                "hook": row.get("hook", ""),
                "clip_type": row.get("clip_type", ""),
                "source_moment_score": row.get("score"),
            }
        )
        if limit is not None and len(entries) >= limit:
            break

    partial_scores: list[dict[str, Any]] = []
    partial_groups: list[dict[str, Any]] = []
    flush_interval = max(1, int(flush_every or getattr(cfg, "SCORER_BATCH_FLUSH_EVERY", 5) or 5))
    manifest_by_clip_id = {
        str(row.get("clip_id") or ""): row
        for row in manifest
        if isinstance(row, dict)
    }

    def _on_group_scored(
        group_record: dict[str, Any],
        variant_scores: list[dict[str, Any]],
        partial_stats: dict[str, Any],
    ) -> None:
        partial_groups.append(group_record)
        partial_scores.extend(variant_scores)
        for variant_score in variant_scores:
            row = manifest_by_clip_id.get(str(variant_score.get("clip_id") or ""))
            if row:
                _attach_score_to_manifest_row(row, variant_score, cfg)
        if len(partial_groups) % flush_interval == 0:
            write_score_artifacts(
                partial_scores,
                folder,
                groups=partial_groups,
                optimization_stats=partial_stats,
                cfg=cfg,
                finalize=False,
            )
            _write_json_atomic(manifest_path, manifest)
            print(
                f"  flushed {len(partial_groups)} base group(s), "
                f"{len(partial_scores)} variant score(s) -> {folder / 'scores_summary.json'}"
            )

    scores, groups, stats = score_clip_variants(
        entries,
        cfg=cfg,
        print_progress=True,
        on_group_scored=_on_group_scored,
    )
    scores_by_clip_id = {
        str(score.get("clip_id")): score
        for score in scores
        if isinstance(score, dict) and score.get("clip_id")
    }
    for row in manifest:
        if not isinstance(row, dict):
            continue
        score = scores_by_clip_id.get(str(row.get("clip_id") or ""))
        if score:
            _attach_score_to_manifest_row(row, score, cfg)

    if scores:
        artifacts = write_score_artifacts(scores, folder, groups=groups, optimization_stats=stats, cfg=cfg)
        _apply_tier_move_stats_to_manifest(manifest, artifacts.get("tier_move", {}))
        _write_json_atomic(manifest_path, manifest)
    return scores


def score_output_tree(
    output_root: str | Path,
    working_root: str | Path | None = None,
    cfg=None,
    limit: int | None = None,
    include_failed: bool = False,
    flush_every: int | None = None,
) -> list[dict[str, Any]]:
    """Score one output folder, or every direct child folder with a manifest."""
    if cfg is None:
        import config as cfg  # type: ignore

    root = Path(output_root)
    if (root / "manifest.json").exists():
        working_dir = working_root
        return score_output_folder(
            root,
            working_dir=working_dir,
            cfg=cfg,
            limit=limit,
            include_failed=include_failed,
            flush_every=flush_every,
        )

    all_scores: list[dict[str, Any]] = []
    for folder in sorted((item for item in root.iterdir() if item.is_dir()), key=lambda item: item.name.casefold()):
        if not (folder / "manifest.json").exists():
            continue
        if limit is not None and len(all_scores) >= limit:
            break
        remaining = None if limit is None else max(0, limit - len(all_scores))
        inferred_working = None
        if working_root:
            inferred_working = Path(working_root) / folder.name
        print(f"\nScoring folder: {folder}")
        folder_scores = score_output_folder(
            folder,
            working_dir=inferred_working,
            cfg=cfg,
            limit=remaining,
            include_failed=include_failed,
            flush_every=flush_every,
        )
        all_scores.extend(folder_scores)

    if all_scores:
        write_score_artifacts(all_scores, root, cfg=cfg)
    return all_scores


def _normalize_score_entry(item: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    clip_path = Path(str(item.get("clip_path") or ""))
    if not clip_path.exists() or not clip_path.is_file():
        return None
    clip_id = str(item.get("clip_id") or clip_path.stem)
    output_file = str(item.get("output_file") or clip_path.name)
    base_clip_id = str(item.get("base_clip_id") or _base_clip_id_from_clip_id(clip_id) or clip_id)
    variant_id = str(item.get("variant_id") or _variant_id_from_clip_id(clip_id, base_clip_id, item) or "")
    variant_index = _variant_index_from_variant_id(variant_id, item.get("version_dir"))
    entry_key = _score_entry_key(clip_path, clip_id)
    return {
        **item,
        "entry_key": entry_key,
        "clip_path": clip_path,
        "clip_id": clip_id,
        "output_file": output_file,
        "base_clip_id": base_clip_id,
        "variant_id": variant_id,
        "variant_index": variant_index,
        "version_dir": str(item.get("version_dir") or _version_dir_from_output_file(output_file)),
        "product": str(item.get("product") or "general"),
        "transcript": item.get("transcript", ""),
    }


def _load_valid_cached_score_for_clip(
    clip_path: Path,
    clip_id: str,
    output_file: str,
    cfg,
    current_weights: dict[str, float],
) -> dict[str, Any] | None:
    entry = {
        "clip_path": Path(clip_path),
        "clip_id": str(clip_id or Path(clip_path).stem),
        "output_file": str(output_file or Path(clip_path).name),
    }
    return _load_valid_cached_score_for_entry(entry, cfg, current_weights)


def _load_valid_cached_score_for_entry(
    entry: dict[str, Any],
    cfg,
    current_weights: dict[str, float],
) -> dict[str, Any] | None:
    if not bool(getattr(cfg, "SCORER_CACHE_ENABLED", True)):
        return None
    if bool(getattr(cfg, "SCORER_FORCE_RESCORE", False)):
        return None

    clip_path = Path(entry["clip_path"])
    scores_path = clip_path.parent / "scores.json"
    if not scores_path.exists():
        return None

    try:
        clip_stat = clip_path.stat()
        score_stat = scores_path.stat()
    except OSError:
        return None
    if score_stat.st_mtime_ns < clip_stat.st_mtime_ns:
        return None

    try:
        payload = json.loads(scores_path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("Could not read score cache %s: %s", scores_path, exc)
        return None

    if not isinstance(payload, dict):
        return None
    if int(payload.get("schema_version") or -1) != SCORE_SCHEMA_VERSION:
        return None

    clips = payload.get("clips", [])
    if not isinstance(clips, list):
        return None

    for score in clips:
        if not isinstance(score, dict):
            continue
        if int(score.get("schema_version") or -1) != SCORE_SCHEMA_VERSION:
            continue
        if not _score_weights_match(score.get("weights"), current_weights):
            continue
        if _cached_score_matches_entry(score, entry, clip_path):
            cached = copy.deepcopy(score)
            _strip_host_focus_from_cached_text_score(cached, cfg)
            cached["cache_hit"] = True
            cached["cache_source"] = str(scores_path.resolve())
            cached["clip_mtime_ns"] = clip_stat.st_mtime_ns
            return cached
    return None


def _cached_score_matches_entry(score: dict[str, Any], entry: dict[str, Any], clip_path: Path) -> bool:
    entry_clip_id = str(entry.get("clip_id") or "")
    entry_output_file = _normalize_output_file_for_match(entry.get("output_file"))
    score_output_file = _normalize_output_file_for_match(score.get("output_file"))
    if entry_clip_id and str(score.get("clip_id") or "") == entry_clip_id:
        return True
    if entry_output_file and score_output_file == entry_output_file:
        return True
    score_path = str(score.get("clip_path") or "")
    if score_path:
        try:
            return Path(score_path).resolve() == clip_path.resolve()
        except OSError:
            return Path(score_path) == clip_path
    return False


def _normalize_output_file_for_match(value: Any) -> str:
    return str(value or "").replace("\\", "/").strip().casefold()


def _score_weights_match(raw_weights: Any, current_weights: dict[str, float]) -> bool:
    if not isinstance(raw_weights, dict):
        return False
    cached_weights = _normalized_dimension_weights(raw_weights)
    if set(cached_weights) != set(current_weights):
        return False
    return all(abs(cached_weights[key] - current_weights[key]) <= 0.00001 for key in current_weights)


def _strip_host_focus_from_cached_text_score(score: dict[str, Any], cfg) -> None:
    has_host_focus = score.get("host_focus_score") is not None
    weights = score.get("weights", {})
    has_host_weight = isinstance(weights, dict) and _safe_float(weights.get("host_focus"), default=0.0) > 0.0
    scored_dimensions = score.get("scored_dimensions", {})
    has_host_dimension = isinstance(scored_dimensions, dict) and bool(scored_dimensions.get("host_focus"))
    if not (has_host_focus or has_host_weight or has_host_dimension):
        return
    _apply_host_focus_result_to_score(
        score,
        {
            "score": None,
            "flags": [],
            "metrics": {
                "enabled": False,
                "reason": "stripped_from_cached_score_for_text_stage",
            },
        },
        cfg,
    )


def _base_score_from_cached_score(score: dict[str, Any]) -> dict[str, Any]:
    base_score = copy.deepcopy(score)
    for key in (
        "similarity_score",
        "similarity_flags",
        "variant_id",
        "variant_index",
        "version_dir",
        "status",
    ):
        base_score.pop(key, None)
    flags = [flag for flag in list(base_score.get("flags", [])) if _clean_flag(flag) != "filtered_by_top_n"]
    base_score["flags"] = _dedupe_flags(flags)
    base_score["cache_hit"] = True
    return base_score


def _prepare_cached_variant_score(score: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    cached = copy.deepcopy(score)
    clip_path = Path(entry["clip_path"])
    cached.update(
        {
            "schema_version": SCORE_SCHEMA_VERSION,
            "score_level": "variant",
            "clip_id": entry.get("clip_id"),
            "base_clip_id": entry.get("base_clip_id"),
            "variant_id": entry.get("variant_id"),
            "variant_index": entry.get("variant_index"),
            "version_dir": entry.get("version_dir"),
            "output_file": entry.get("output_file"),
            "clip_path": str(clip_path.resolve()),
            "output_dir": str(clip_path.parent.parent.resolve())
            if clip_path.parent.name.startswith("v")
            else str(clip_path.parent.resolve()),
            "product": entry.get("product", cached.get("product", "general")),
            "hook": entry.get("hook", cached.get("hook", "")),
            "clip_type": entry.get("clip_type", cached.get("clip_type", "")),
            "source_moment_score": entry.get("source_moment_score", cached.get("source_moment_score")),
            "compliance_passed": entry.get("compliance_passed", cached.get("compliance_passed")),
            "violation_count": entry.get("violation_count", cached.get("violation_count")),
            "auto_fixed": entry.get("auto_fixed", cached.get("auto_fixed")),
            "compliance_blocked": entry.get("compliance_blocked", cached.get("compliance_blocked")),
            "compliance_summary": entry.get("compliance_summary", cached.get("compliance_summary", "")),
            "compliance_file": entry.get("compliance_file", cached.get("compliance_file", "")),
            "cache_hit": True,
            "clip_mtime_ns": _clip_mtime_ns(clip_path),
        }
    )
    cached["flags"] = _dedupe_flags(list(cached.get("flags", [])))
    cached["similarity_flags"] = _dedupe_flags(list(cached.get("similarity_flags", [])))
    return cached


def _score_entry_key(clip_path: Path, clip_id: str) -> str:
    raw = f"{clip_id}|{clip_path.resolve()}"
    return re.sub(r"[^a-zA-Z0-9_]+", "_", raw)[-180:]


def _group_score_entries_by_base_clip(entries: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        key = str(entry.get("base_clip_id") or entry.get("clip_id") or "")
        groups.setdefault(key, []).append(entry)
    for key in list(groups):
        groups[key] = sorted(
            groups[key],
            key=lambda item: (
                _safe_float(item.get("variant_index"), default=9999.0),
                str(item.get("variant_id") or ""),
                str(item.get("clip_id") or ""),
            ),
        )
    return dict(sorted(groups.items(), key=lambda item: item[0]))


def _select_base_score_entry(entries: list[dict[str, Any]]) -> dict[str, Any]:
    def rank(entry: dict[str, Any]) -> tuple[int, float, str]:
        variant_id = str(entry.get("variant_id") or "").casefold()
        clip_id = str(entry.get("clip_id") or "").casefold()
        version_dir = str(entry.get("version_dir") or "").casefold()
        is_original = "original" in variant_id or "original" in clip_id
        is_v0 = variant_id.startswith("v0") or version_dir == "v0" or "_v0" in clip_id
        return (
            0 if is_original and is_v0 else 1 if is_v0 else 2,
            _safe_float(entry.get("variant_index"), default=9999.0),
            str(entry.get("clip_id") or ""),
        )

    return sorted(entries, key=rank)[0]


def _base_clip_id_from_clip_id(clip_id: str) -> str:
    text = str(clip_id or "").strip()
    match = re.match(r"^(clip_\d+)(?:_v\d+(?:_|$).*)?$", text, flags=re.IGNORECASE)
    if match:
        return match.group(1)
    match = re.match(r"^(.+?)_v\d+(?:_|$).*$", text, flags=re.IGNORECASE)
    if match:
        return match.group(1)
    return text


def _variant_id_from_clip_id(clip_id: str, base_clip_id: str, item: dict[str, Any]) -> str:
    explicit = item.get("variant_id")
    if explicit:
        return str(explicit)
    text = str(clip_id or "")
    base = str(base_clip_id or "")
    if base and text.startswith(base + "_"):
        return text[len(base) + 1 :]
    match = re.search(r"(v\d+(?:_.*)?)$", text, flags=re.IGNORECASE)
    if match:
        return match.group(1)
    version_dir = str(item.get("version_dir") or _version_dir_from_output_file(item.get("output_file")) or "")
    return f"{version_dir}_variant" if version_dir else "original"


def _variant_index_from_variant_id(variant_id: str, version_dir: Any = None) -> int:
    for value in (variant_id, version_dir):
        match = re.match(r"^v(\d+)(?:_|$)", str(value or ""), flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
    return 0


def _version_dir_from_output_file(output_file: Any) -> str:
    text = str(output_file or "").replace("\\", "/")
    if "/" not in text:
        return ""
    return text.split("/", 1)[0]


def _copy_compliance_fields(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key in (
        "compliance_passed",
        "violation_count",
        "auto_fixed",
        "compliance_blocked",
        "compliance_summary",
        "compliance_file",
    ):
        if key in source:
            target[key] = source[key]


def _decorate_base_score(
    base_score: dict[str, Any],
    base_clip_id: str,
    representative: dict[str, Any],
    group_entries: list[dict[str, Any]],
) -> None:
    rep_path = Path(representative["clip_path"])
    base_score["schema_version"] = SCORE_SCHEMA_VERSION
    base_score["score_level"] = "base"
    base_score["clip_id"] = base_clip_id
    base_score["base_clip_id"] = base_clip_id
    base_score["representative_clip_id"] = representative.get("clip_id")
    base_score["representative_variant_id"] = representative.get("variant_id")
    base_score["representative_output_file"] = representative.get("output_file")
    base_score["representative_clip_path"] = str(rep_path.resolve())
    base_score["output_dir"] = str(rep_path.parent.parent.resolve()) if rep_path.parent.name.startswith("v") else str(rep_path.parent.resolve())
    base_score["variant_count"] = len(group_entries)
    base_score["variants_scored_from_base"] = True
    base_score["hook"] = representative.get("hook", base_score.get("hook", ""))
    base_score["clip_type"] = representative.get("clip_type", base_score.get("clip_type", ""))
    base_score["source_moment_score"] = representative.get("source_moment_score", base_score.get("source_moment_score"))
    _copy_compliance_fields(base_score, representative)


def _failed_base_score(base_clip_id: str, representative: dict[str, Any], exc: Exception, cfg) -> dict[str, Any]:
    weights = _normalized_dimension_weights(getattr(cfg, "SCORER_WEIGHTS", None))
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    return {
        "schema_version": SCORE_SCHEMA_VERSION,
        "clip_id": base_clip_id,
        "output_file": representative.get("output_file", ""),
        "clip_path": str(Path(representative["clip_path"]).resolve()),
        "product": representative.get("product", "general"),
        "content_score": None,
        "visual_score": None,
        "host_focus_score": None,
        "hook_score": None,
        "hook_summary": "",
        "quality_score": None,
        "engagement_score": None,
        "total_score": 0.0,
        "weights": weights,
        "scored_dimensions": {
            "content": False,
            "quality": False,
            "engagement": False,
            "host_focus": False,
        },
        "flags": ["base_scoring_failed"],
        "score_caps_applied": [],
        "summary": f"Skor dasar gagal dihitung: {str(exc)[:120]}",
        "metrics": {"base_error": str(exc)},
        "export_threshold": float(getattr(cfg, "SCORER_MIN_SCORE_TO_EXPORT", 0.0) or 0.0),
        "exported": False,
        "scored_at": now,
    }


def _score_similarity_for_group(entries: list[dict[str, Any]], cfg) -> dict[str, dict[str, Any]]:
    if len(entries) <= 1:
        return {
            entry["entry_key"]: {
                "score": None,
                "flags": ["similarity_no_siblings"],
                "metrics": {"sibling_count": 0},
            }
            for entry in entries
        }

    try:
        import cv2
    except ImportError as exc:
        return _similarity_unavailable(entries, f"opencv-python unavailable: {exc}")

    frame_sample_rate = max(
        1,
        int(getattr(cfg, "SCORER_SIMILARITY_FRAME_SAMPLE_RATE", 30) or 30),
    )
    max_frames = max(1, int(getattr(cfg, "SCORER_SIMILARITY_MAX_FRAMES", 24) or 24))
    histograms: dict[str, dict[str, Any]] = {}
    for entry in entries:
        try:
            histograms[entry["entry_key"]] = _sample_frame_histograms(
                cv2,
                Path(entry["clip_path"]),
                frame_sample_rate,
                max_frames,
            )
        except Exception as exc:
            histograms[entry["entry_key"]] = {
                "histograms": [],
                "sampled_frames": 0,
                "error": str(exc),
            }

    output: dict[str, dict[str, Any]] = {}
    for entry in entries:
        entry_key = entry["entry_key"]
        own = histograms.get(entry_key, {})
        own_hists = own.get("histograms", [])
        pair_scores = []
        sibling_details = []
        if own_hists:
            for sibling in entries:
                sibling_key = sibling["entry_key"]
                if sibling_key == entry_key:
                    continue
                sibling_hists = histograms.get(sibling_key, {}).get("histograms", [])
                distinctness = _compare_histogram_sequences(cv2, own_hists, sibling_hists)
                if distinctness is None:
                    continue
                pair_scores.append(distinctness)
                sibling_details.append(
                    {
                        "clip_id": sibling.get("clip_id"),
                        "distinctness": round(distinctness, 4),
                    }
                )

        if pair_scores:
            avg_distinctness = sum(pair_scores) / len(pair_scores)
            score = _round_score(avg_distinctness * 10.0)
            flags = []
            if score >= 7:
                flags.append("visually_distinct_variant")
            elif score < 3:
                flags.append("visually_similar_variant")
            output[entry_key] = {
                "score": score,
                "flags": flags,
                "metrics": {
                    "frame_sample_rate": frame_sample_rate,
                    "max_frames": max_frames,
                    "sampled_frames": own.get("sampled_frames", len(own_hists)),
                    "sibling_count": len(entries) - 1,
                    "average_distinctness": round(avg_distinctness, 4),
                    "siblings": sibling_details,
                    "error": own.get("error"),
                },
            }
        else:
            output[entry_key] = {
                "score": None,
                "flags": ["similarity_no_frames" if not own_hists else "similarity_unavailable"],
                "metrics": {
                    "frame_sample_rate": frame_sample_rate,
                    "max_frames": max_frames,
                    "sampled_frames": own.get("sampled_frames", 0),
                    "sibling_count": len(entries) - 1,
                    "error": own.get("error"),
                },
            }
    return output


def _similarity_unavailable(entries: list[dict[str, Any]], error: str) -> dict[str, dict[str, Any]]:
    return {
        entry["entry_key"]: {
            "score": None,
            "flags": ["similarity_unavailable"],
            "metrics": {"error": error, "sibling_count": max(0, len(entries) - 1)},
        }
        for entry in entries
    }


def _sample_frame_histograms(cv2_module, clip_path: Path, frame_sample_rate: int, max_frames: int) -> dict[str, Any]:
    cap = cv2_module.VideoCapture(str(clip_path))
    if not cap.isOpened():
        raise RuntimeError(f"cannot open clip for similarity: {clip_path}")
    total_frames = int(cap.get(cv2_module.CAP_PROP_FRAME_COUNT) or 0)
    fps = float(cap.get(cv2_module.CAP_PROP_FPS) or 0.0)
    histograms = []
    frame_idx = 0
    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            if frame_idx % frame_sample_rate == 0:
                resized = cv2_module.resize(frame, (160, 160), interpolation=cv2_module.INTER_AREA)
                hsv = cv2_module.cvtColor(resized, cv2_module.COLOR_BGR2HSV)
                hist = cv2_module.calcHist([hsv], [0, 1], None, [32, 32], [0, 180, 0, 256])
                cv2_module.normalize(hist, hist, alpha=0, beta=1, norm_type=cv2_module.NORM_MINMAX)
                histograms.append(hist.astype("float32"))
                if len(histograms) >= max_frames:
                    break
            frame_idx += 1
    finally:
        cap.release()
    return {
        "histograms": histograms,
        "sampled_frames": len(histograms),
        "total_frames": total_frames,
        "fps": round(fps, 3) if fps > 0 else None,
    }


def _compare_histogram_sequences(cv2_module, first: list[Any], second: list[Any]) -> float | None:
    count = min(len(first), len(second))
    if count <= 0:
        return None
    distances = []
    for idx in range(count):
        corr = cv2_module.compareHist(first[idx], second[idx], cv2_module.HISTCMP_CORREL)
        if not math.isfinite(float(corr)):
            continue
        similarity = max(0.0, min(1.0, (float(corr) + 1.0) / 2.0))
        distances.append(1.0 - similarity)
    if not distances:
        return None
    return max(0.0, min(1.0, sum(distances) / len(distances)))


def _inherit_base_score_for_variant(
    base_score: dict[str, Any],
    entry: dict[str, Any],
    similarity: dict[str, Any],
) -> dict[str, Any]:
    score = copy.deepcopy(base_score)
    similarity_score = _round_optional_score(similarity.get("score")) if similarity else None
    similarity_flags = _dedupe_flags(list(similarity.get("flags", []))) if similarity else []
    score.update(
        {
            "schema_version": SCORE_SCHEMA_VERSION,
            "score_level": "variant",
            "clip_id": entry.get("clip_id"),
            "base_clip_id": entry.get("base_clip_id"),
            "variant_id": entry.get("variant_id"),
            "variant_index": entry.get("variant_index"),
            "version_dir": entry.get("version_dir"),
            "output_file": entry.get("output_file"),
            "clip_path": str(Path(entry["clip_path"]).resolve()),
            "output_dir": str(Path(entry["clip_path"]).parent.parent.resolve())
            if Path(entry["clip_path"]).parent.name.startswith("v")
            else str(Path(entry["clip_path"]).parent.resolve()),
            "hook": entry.get("hook", score.get("hook", "")),
            "clip_type": entry.get("clip_type", score.get("clip_type", "")),
            "source_moment_score": entry.get("source_moment_score", score.get("source_moment_score")),
            "compliance_passed": entry.get("compliance_passed", score.get("compliance_passed")),
            "violation_count": entry.get("violation_count", score.get("violation_count")),
            "auto_fixed": entry.get("auto_fixed", score.get("auto_fixed")),
            "compliance_blocked": entry.get("compliance_blocked", score.get("compliance_blocked")),
            "compliance_summary": entry.get("compliance_summary", score.get("compliance_summary", "")),
            "compliance_file": entry.get("compliance_file", score.get("compliance_file", "")),
            "similarity_score": similarity_score,
            "similarity_flags": similarity_flags,
            "inherited_base_scores": True,
            "inherited_from_base_clip_id": entry.get("base_clip_id"),
            "inherited_from_representative_clip_id": base_score.get("representative_clip_id"),
        }
    )
    score["flags"] = _dedupe_flags(list(score.get("flags", [])) + similarity_flags)
    metrics = score.get("metrics", {})
    if not isinstance(metrics, dict):
        metrics = {}
    metrics["similarity"] = similarity.get("metrics", {}) if similarity else {}
    score["metrics"] = metrics
    return score


def _apply_top_variant_filter_to_variant_scores(
    variant_scores: list[dict[str, Any]],
    cfg,
) -> None:
    for score in variant_scores:
        flags = [
            flag
            for flag in list(score.get("flags", []))
            if _clean_flag(flag) != "filtered_by_top_n"
        ]
        score["flags"] = _dedupe_flags(flags)
        if score.get("status") == "filtered_low_variant":
            score.pop("status", None)

    try:
        top_n = int(getattr(cfg, "SCORER_TOP_VARIANTS_PER_CLIP", 0) or 0)
    except (TypeError, ValueError):
        top_n = 0
    if top_n <= 0 or len(variant_scores) <= top_n:
        return

    ranked = sorted(
        enumerate(variant_scores),
        key=lambda item: (
            -_safe_float(item[1].get("total_score"), default=-1.0),
            _safe_float(item[1].get("variant_index"), default=9999.0),
            str(item[1].get("clip_id") or ""),
        ),
    )
    kept_indexes = {index for index, _score in ranked[:top_n]}
    for index, score in enumerate(variant_scores):
        if index in kept_indexes:
            continue
        score["status"] = "filtered_low_variant"
        score["flags"] = _dedupe_flags(list(score.get("flags", [])) + ["filtered_by_top_n"])


def _build_group_score_from_base(base_score: dict[str, Any], variant_scores: list[dict[str, Any]]) -> dict[str, Any]:
    variants = [_variant_summary_from_score(score) for score in _sort_variant_scores(variant_scores)]
    similarity_values = [
        _safe_float(variant.get("similarity_score"), default=float("nan"))
        for variant in variants
        if variant.get("similarity_score") is not None
    ]
    similarity_values = [value for value in similarity_values if math.isfinite(value)]
    average_similarity = (
        _round_score(sum(similarity_values) / len(similarity_values))
        if similarity_values
        else None
    )
    return {
        "schema_version": SCORE_SCHEMA_VERSION,
        "score_level": "base",
        "clip_id": base_score.get("base_clip_id") or base_score.get("clip_id"),
        "base_clip_id": base_score.get("base_clip_id") or base_score.get("clip_id"),
        "product": base_score.get("product", "general"),
        "content_score": base_score.get("content_score"),
        "visual_score": base_score.get("visual_score"),
        "host_focus_score": base_score.get("host_focus_score"),
        "hook_score": base_score.get("hook_score"),
        "hook_summary": base_score.get("hook_summary", ""),
        "quality_score": base_score.get("quality_score"),
        "engagement_score": base_score.get("engagement_score"),
        "total_score": base_score.get("total_score"),
        "weights": base_score.get("weights", {}),
        "scored_dimensions": base_score.get("scored_dimensions", {}),
        "flags": base_score.get("flags", []),
        "score_caps_applied": base_score.get("score_caps_applied", []),
        "summary": base_score.get("summary", ""),
        "metrics": base_score.get("metrics", {}),
        "hook": base_score.get("hook", ""),
        "clip_type": base_score.get("clip_type", ""),
        "source_moment_score": base_score.get("source_moment_score"),
        "compliance_passed": base_score.get("compliance_passed"),
        "violation_count": base_score.get("violation_count"),
        "auto_fixed": base_score.get("auto_fixed"),
        "compliance_blocked": base_score.get("compliance_blocked"),
        "compliance_summary": base_score.get("compliance_summary", ""),
        "compliance_file": base_score.get("compliance_file", ""),
        "representative_clip_id": base_score.get("representative_clip_id"),
        "representative_variant_id": base_score.get("representative_variant_id"),
        "representative_output_file": base_score.get("representative_output_file"),
        "representative_clip_path": base_score.get("representative_clip_path"),
        "output_dir": base_score.get("output_dir"),
        "variant_count": len(variants),
        "average_similarity_score": average_similarity,
        "export_threshold": base_score.get("export_threshold"),
        "exported": bool(base_score.get("exported", True)),
        "scored_at": base_score.get("scored_at", ""),
        "variants": variants,
    }


def _variant_summary_from_score(score: dict[str, Any]) -> dict[str, Any]:
    return {
        "clip_id": score.get("clip_id"),
        "base_clip_id": score.get("base_clip_id"),
        "variant_id": score.get("variant_id"),
        "variant_index": score.get("variant_index"),
        "version_dir": score.get("version_dir"),
        "output_file": score.get("output_file"),
        "clip_path": score.get("clip_path"),
        "status": score.get("status"),
        "flags": score.get("flags", []),
        "similarity_score": score.get("similarity_score"),
        "similarity_flags": score.get("similarity_flags", []),
        "similarity_metrics": (score.get("metrics") or {}).get("similarity", {}),
        "compliance_passed": score.get("compliance_passed"),
        "violation_count": score.get("violation_count"),
        "auto_fixed": score.get("auto_fixed"),
        "compliance_blocked": score.get("compliance_blocked"),
        "compliance_summary": score.get("compliance_summary", ""),
        "compliance_file": score.get("compliance_file", ""),
        "exported": bool(score.get("exported", True)),
        "scored_at": score.get("scored_at", ""),
    }


def _build_score_groups_from_flat_scores(scores: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for score in scores:
        base_clip_id = str(score.get("base_clip_id") or _base_clip_id_from_clip_id(str(score.get("clip_id") or "")))
        output_dir = str(score.get("output_dir") or _output_dir_from_score(score))
        grouped.setdefault((output_dir, base_clip_id), []).append(score)

    groups = []
    for (_, base_clip_id), group_scores in grouped.items():
        representative = _select_representative_score(group_scores)
        base_score = copy.deepcopy(representative)
        base_score["score_level"] = "base"
        base_score["clip_id"] = base_clip_id
        base_score["base_clip_id"] = base_clip_id
        base_score.setdefault("representative_clip_id", representative.get("clip_id"))
        base_score.setdefault("representative_output_file", representative.get("output_file"))
        base_score.setdefault("representative_clip_path", representative.get("clip_path"))
        base_score["variant_count"] = len(group_scores)
        groups.append(_build_group_score_from_base(base_score, group_scores))
    return groups


def _select_representative_score(scores: list[dict[str, Any]]) -> dict[str, Any]:
    def rank(score: dict[str, Any]) -> tuple[int, float, str]:
        variant_id = str(score.get("variant_id") or "").casefold()
        clip_id = str(score.get("clip_id") or "").casefold()
        version_dir = str(score.get("version_dir") or "").casefold()
        is_original = "original" in variant_id or "original" in clip_id
        is_v0 = variant_id.startswith("v0") or version_dir == "v0" or "_v0" in clip_id
        return (
            0 if is_original and is_v0 else 1 if is_v0 else 2,
            _safe_float(score.get("variant_index"), default=9999.0),
            str(score.get("clip_id") or ""),
        )

    return sorted(scores, key=rank)[0]


def _sort_variant_scores(scores: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        scores,
        key=lambda score: (
            _safe_float(score.get("variant_index"), default=9999.0),
            str(score.get("variant_id") or ""),
            str(score.get("clip_id") or ""),
        ),
    )


def _output_dir_from_score(score: dict[str, Any]) -> str:
    clip_path = Path(str(score.get("clip_path") or ""))
    if clip_path.parent.name.startswith("v"):
        return str(clip_path.parent.parent)
    return str(clip_path.parent)


def _public_score(score: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in score.items()
        if not str(key).startswith("_") and key not in {"transcript"}
    }


def _public_group(group: dict[str, Any]) -> dict[str, Any]:
    clean = _public_score(group)
    variants = clean.get("variants", [])
    if isinstance(variants, list):
        clean["variants"] = [_public_score(item) for item in variants if isinstance(item, dict)]
    return clean


def _build_scoring_optimization_stats(
    scores: list[dict[str, Any]],
    groups: list[dict[str, Any]],
) -> dict[str, Any]:
    previous_calls = len(scores)
    actual_calls = len(groups)
    return {
        "previous_scoring_calls": previous_calls,
        "actual_scoring_calls": actual_calls,
        "saved_scoring_calls": max(0, previous_calls - actual_calls),
        "base_clip_count": actual_calls,
        "variant_clip_count": previous_calls,
        "cached_score_count": 0,
        "fresh_score_count": previous_calls,
        "cache_miss_count": previous_calls,
        "previous_text_qwen_calls": actual_calls * 2,
        "actual_text_qwen_calls": actual_calls,
        "saved_text_qwen_calls": actual_calls,
        "saved_text_qwen_calls_from_merge": actual_calls,
    }


def _score_content_and_engagement_with_qwen(
    transcript_text: str,
    product_name: str,
    cfg,
) -> dict[str, Any]:
    if not transcript_text.strip():
        engagement_score, engagement_flags, engagement_metrics = _score_engagement(
            transcript_text,
            product_name,
            cfg,
        )
        return {
            "content": {
                "score": 0.0,
                "flags": ["no_transcript", "off_topic"],
                "summary": "Transkrip kosong, jadi relevansi produk tidak bisa dinilai.",
                "metrics": {"source": "empty_transcript"},
            },
            "engagement": {
                "score": engagement_score,
                "flags": engagement_flags,
                "metrics": {
                    **engagement_metrics,
                    "source": "keyword_fallback",
                    "fallback_reason": "empty_transcript",
                },
            },
        }

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("openai package is not installed") from exc

    client = OpenAI(
        base_url=getattr(cfg, "LM_STUDIO_BASE_URL", "http://localhost:1234/v1"),
        api_key=getattr(cfg, "LM_STUDIO_API_KEY", "lm-studio"),
    )
    prompt_text = transcript_text[:8000]
    messages = [
        {
            "role": "system",
            "content": (
                "Kamu adalah evaluator kualitas clip livestream skincare PROYA. "
                "Nilai hanya berdasarkan transkrip Bahasa Indonesia. "
                "Semua instruksi, alasan, dan ringkasan harus memakai Bahasa Indonesia. "
                "Kembalikan hanya JSON valid tanpa markdown, tanpa komentar, dan tanpa teks tambahan."
            ),
        },
        {
            "role": "user",
            "content": (
                "Produk target: "
                f"{product_name or 'general'}\n\n"
                "Tugas:\n"
                "1. Beri content_score 0 sampai 10 untuk seberapa fokus pembicaraan pada produk target, "
                "benefit, cara pakai, kandungan, promo, atau masalah kulit yang diselesaikan produk.\n"
                "2. Turunkan skor jika host keluar topik, hanya menyapa chat, banyak filler, atau produk target tidak jelas.\n"
                "3. Jika transkrip hanya membahas harga, promo, voucher, ongkir, checkout, atau diskon "
                "tanpa benefit, cara pakai, kandungan, demo, atau penjelasan produk, content_score maksimal 4.0.\n"
                "4. Flags harus menggambarkan fokus utama, bukan label umum. "
                "Gunakan hanya yang relevan dari daftar ini: promo_focus, demo_focus, benefit_focus, "
                "ingredient_focus, product_focus, promo_price_only, off_topic, filler_heavy, product_ambiguous. "
                "Jika clip hanya fokus promo, jangan pakai product_relevant atau benefit_claim; cukup promo_focus. "
                "Jika membahas beberapa fokus, sertakan semua fokus yang benar-benar muncul.\n"
                "5. Beri content_summary satu kalimat Bahasa Indonesia saja yang menjelaskan alasan skor.\n"
                "6. Beri engagement_score 0 sampai 10 untuk kekuatan ajakan beli/menonton berdasarkan "
                "harga atau promo, penyebutan nama produk, sinyal demo, dan klaim benefit.\n"
                "7. engagement_flags harus menjelaskan sinyal engagement yang muncul, misalnya "
                "promo_focus, product_focus, demo_focus, benefit_focus, ingredient_focus, atau off_topic.\n"
                "8. engagement_metrics wajib berisi boolean untuk price_mentioned, product_name_mentioned, "
                "demo_signal, dan benefit_claim.\n\n"
                "Format JSON wajib:\n"
                "{"
                "\"content_score\": 0.0, "
                "\"content_flags\": [\"flag\"], "
                "\"content_summary\": \"satu kalimat Bahasa Indonesia\", "
                "\"engagement_score\": 0.0, "
                "\"engagement_flags\": [\"flag\"], "
                "\"engagement_metrics\": {"
                "\"price_mentioned\": false, "
                "\"product_name_mentioned\": false, "
                "\"demo_signal\": false, "
                "\"benefit_claim\": false"
                "}"
                "}\n\n"
                "Transkrip clip:\n"
                f"{prompt_text}"
            ),
        },
    ]
    response = client.chat.completions.create(
        model=getattr(cfg, "LM_STUDIO_MODEL", "qwen/qwen3.6-27b"),
        messages=messages,
        temperature=0.0,
        max_tokens=512,
        timeout=min(float(getattr(cfg, "LM_STUDIO_TIMEOUT", 120)), 120.0),
    )
    raw = (response.choices[0].message.content or "").strip()
    payload = _parse_json_object(raw)
    focus_counts = _content_signal_counts(transcript_text, product_name, cfg)

    content_score = _round_score(_safe_float(payload.get("content_score"), default=0.0))
    summary = str(payload.get("content_summary") or payload.get("summary") or "").strip()
    raw_content_flags = payload.get("content_flags", payload.get("flags", []))
    if not isinstance(raw_content_flags, list):
        raw_content_flags = [raw_content_flags]
    content_flags = _normalize_content_focus_flags(raw_content_flags, focus_counts, content_score)
    content_score_before_penalty = content_score
    content_score, content_flags, promo_price_only_penalty = _apply_promo_price_only_content_penalty(
        content_score,
        content_flags,
        focus_counts,
    )
    if not summary:
        summary = _fallback_summary({"content": content_score}, content_flags)

    fallback_engagement_score, fallback_engagement_flags, fallback_engagement_metrics = _score_engagement(
        transcript_text,
        product_name,
        cfg,
    )
    engagement_from_qwen = "engagement_score" in payload
    engagement_score = (
        _round_score(_safe_float(payload.get("engagement_score"), default=fallback_engagement_score))
        if engagement_from_qwen
        else fallback_engagement_score
    )
    raw_engagement_flags = payload.get("engagement_flags", [])
    if not isinstance(raw_engagement_flags, list):
        raw_engagement_flags = [raw_engagement_flags]
    engagement_flags = _dedupe_flags(
        list(fallback_engagement_flags)
        + [_clean_flag(flag) for flag in raw_engagement_flags]
    )
    if not engagement_from_qwen:
        engagement_flags = _dedupe_flags(engagement_flags + ["engagement_keyword_fallback"])

    raw_engagement_metrics = payload.get("engagement_metrics", {})
    if not isinstance(raw_engagement_metrics, dict):
        raw_engagement_metrics = {}
    qwen_engagement_metrics = {
        "price_mentioned": _coerce_bool(
            raw_engagement_metrics.get("price_mentioned"),
            bool(fallback_engagement_metrics.get("price_matches")),
        ),
        "product_name_mentioned": _coerce_bool(
            raw_engagement_metrics.get("product_name_mentioned"),
            bool(fallback_engagement_metrics.get("product_mentions")),
        ),
        "demo_signal": _coerce_bool(
            raw_engagement_metrics.get("demo_signal"),
            bool(fallback_engagement_metrics.get("demo_matches")),
        ),
        "benefit_claim": _coerce_bool(
            raw_engagement_metrics.get("benefit_claim"),
            bool(fallback_engagement_metrics.get("benefit_matches")),
        ),
    }

    return {
        "content": {
            "score": content_score,
            "flags": _dedupe_flags(content_flags),
            "summary": summary,
            "metrics": {
                "source": "qwen_combined",
                "raw_response": raw[:1000],
                "focus_counts": focus_counts,
                "promo_price_only_detected": "promo_price_only" in content_flags,
                "promo_price_only_penalty": promo_price_only_penalty,
                "content_score_before_promo_price_only_penalty": content_score_before_penalty,
            },
        },
        "engagement": {
            "score": engagement_score,
            "flags": engagement_flags,
            "metrics": {
                **fallback_engagement_metrics,
                "source": "qwen_combined" if engagement_from_qwen else "keyword_fallback",
                "qwen_metrics": qwen_engagement_metrics,
                "raw_response": raw[:1000],
            },
        },
    }


def _score_hook_with_qwen(transcript: Any, product_name: str, cfg) -> dict[str, Any]:
    hook_text = _first_seconds_transcript_text(transcript, seconds=8.0)
    if hook_text is None:
        return {
            "score": None,
            "summary": "",
            "metrics": {
                "source": "skipped_no_word_timestamps",
                "window_seconds": 8.0,
            },
        }
    if not hook_text.strip():
        return {
            "score": 0.0,
            "summary": "Pembukaan kosong sehingga kekuatan hook tidak bisa dinilai.",
            "metrics": {
                "source": "empty_hook_window",
                "window_seconds": 8.0,
                "text": "",
            },
        }

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("openai package is not installed") from exc

    client = OpenAI(
        base_url=getattr(cfg, "LM_STUDIO_BASE_URL", "http://localhost:1234/v1"),
        api_key=getattr(cfg, "LM_STUDIO_API_KEY", "lm-studio"),
    )
    messages = [
        {
            "role": "system",
            "content": (
                "Kamu adalah evaluator hook pembuka clip livestream skincare PROYA. "
                "Nilai hanya dari transkrip 8 detik pertama. "
                "Kembalikan hanya JSON valid tanpa markdown dan gunakan Bahasa Indonesia."
            ),
        },
        {
            "role": "user",
            "content": (
                "Produk target: "
                f"{product_name or 'general'}\n\n"
                "Tugas:\n"
                "Beri hook_score 0 sampai 10 untuk kekuatan pembukaan. Skor tinggi jika pembukaan "
                "langsung menyebut produk, benefit, harga/promo, atau pertanyaan hook yang membuat penonton berhenti scroll. "
                "Beri hook_summary satu kalimat Bahasa Indonesia.\n\n"
                "Format JSON wajib:\n"
                "{\"hook_score\": 0.0, \"hook_summary\": \"satu kalimat Bahasa Indonesia\"}\n\n"
                "Transkrip 8 detik pertama:\n"
                f"{hook_text[:2000]}"
            ),
        },
    ]
    response = client.chat.completions.create(
        model=getattr(cfg, "LM_STUDIO_MODEL", "qwen/qwen3.6-27b"),
        messages=messages,
        temperature=0.0,
        max_tokens=160,
        timeout=min(float(getattr(cfg, "LM_STUDIO_TIMEOUT", 120)), 120.0),
    )
    raw = (response.choices[0].message.content or "").strip()
    payload = _parse_json_object(raw)
    summary = str(payload.get("hook_summary") or "").strip()
    if not summary:
        summary = "Kekuatan pembukaan dinilai dari sinyal produk, benefit, harga, atau pertanyaan hook."
    return {
        "score": _round_score(_safe_float(payload.get("hook_score"), default=0.0)),
        "summary": summary,
        "metrics": {
            "source": "qwen",
            "window_seconds": 8.0,
            "text": hook_text,
            "raw_response": raw[:500],
        },
    }


def _first_seconds_transcript_text(transcript: Any, seconds: float) -> str | None:
    words = _transcript_words_with_timestamps(transcript)
    if words is None:
        return None
    selected = []
    for word in words:
        start = word.get("start")
        if start is None:
            continue
        if _safe_float(start, default=seconds + 1.0) < seconds:
            text = str(word.get("word") or "").strip()
            if text:
                selected.append(text)
    return " ".join(selected).strip()


def _transcript_words_with_timestamps(transcript: Any) -> list[dict[str, Any]] | None:
    if isinstance(transcript, dict):
        if isinstance(transcript.get("words"), list):
            words = transcript.get("words", [])
        elif isinstance(transcript.get("segments"), list):
            words = []
            for segment in transcript["segments"]:
                if isinstance(segment, dict) and isinstance(segment.get("words"), list):
                    words.extend(segment["words"])
        else:
            return None
    elif isinstance(transcript, list):
        words = transcript
    else:
        return None

    output = []
    for item in words:
        if not isinstance(item, dict):
            return None
        if item.get("start") is None or item.get("end") is None:
            return None
        output.append(item)
    return output


def _score_host_focus_with_qwen_vl(
    clip_path: Path,
    cfg,
    frame_sample_rate: int,
    clip_id: str | None = None,
) -> dict[str, Any]:
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("opencv-python is not installed") from exc

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("openai package is not installed") from exc

    client = OpenAI(
        base_url=getattr(cfg, "SCORER_VISION_BASE_URL", "http://localhost:1235/v1"),
        api_key=getattr(cfg, "SCORER_VISION_API_KEY", "lm-studio"),
    )
    model_name = getattr(cfg, "SCORER_VISION_MODEL", "Qwen2.5-VL-72B-Instruct-Q4")
    timeout = float(getattr(cfg, "SCORER_VISION_TIMEOUT", 120) or 120)

    cap = cv2.VideoCapture(str(clip_path))
    if not cap.isOpened():
        raise RuntimeError(f"cannot open clip: {clip_path}")

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
    frame_idx = 0
    sampled = 0
    skipped_initial_frame = False
    skip_first_frame = bool(getattr(cfg, "SCORER_FOCUS_SKIP_FIRST_FRAME", True))
    counts = {"A": 0, "B": 0, "C": 0, "unknown": 0}
    samples: list[dict[str, Any]] = []
    debug_frames: list[dict[str, Any]] = []

    while True:
        ret, frame = cap.read()
        if not ret:
            break
        if frame_idx % frame_sample_rate != 0:
            frame_idx += 1
            continue
        if skip_first_frame and frame_idx == 0:
            skipped_initial_frame = True
            frame_idx += 1
            continue

        encoded = _encode_frame_as_jpeg_base64(cv2, frame)
        timestamp = (frame_idx / fps) if fps > 0 else None
        label, raw = _classify_host_focus_frame(
            client,
            model_name,
            encoded,
            timeout,
        )
        normalized_label = label if label in counts else "unknown"
        confidence = _host_focus_label_confidence(normalized_label, raw)
        sample = {
            "frame": frame_idx,
            "time": round(timestamp, 3) if timestamp is not None else None,
            "label": normalized_label,
            "raw": raw[:120],
            "confidence": confidence,
        }
        samples.append(sample)
        debug_frames.append({"frame": frame.copy(), "sample": sample})
        sampled += 1
        frame_idx += 1

    cap.release()

    if sampled == 0:
        return {
            "score": None,
            "flags": ["host_focus_no_frames"],
            "metrics": {
                "enabled": True,
                "sampled_frames": 0,
                "frame_sample_rate": frame_sample_rate,
                "skip_first_frame": skip_first_frame,
                "skipped_initial_frame": skipped_initial_frame,
            },
        }

    scoring_samples = samples
    outlier_dropped = None
    if bool(getattr(cfg, "SCORER_FOCUS_DROP_OUTLIERS", True)) and sampled >= 6:
        outlier_dropped = _select_host_focus_outlier(samples)
        if outlier_dropped is not None:
            samples[outlier_dropped]["outlier_dropped"] = True
            scoring_samples = [
                sample for idx, sample in enumerate(samples) if idx != outlier_dropped
            ]

    scoring_counts = {"A": 0, "B": 0, "C": 0, "unknown": 0}
    for sample in scoring_samples:
        label = str(sample.get("label") or "unknown")
        scoring_counts[label if label in scoring_counts else "unknown"] += 1
    for sample in samples:
        label = str(sample.get("label") or "unknown")
        counts[label if label in counts else "unknown"] += 1

    total_scored = max(1, len(scoring_samples))
    focus_ratio = scoring_counts["A"] / total_scored
    phone_ratio = scoring_counts["B"] / total_scored
    other_ratio = scoring_counts["C"] / total_scored
    unknown_ratio = scoring_counts["unknown"] / total_scored
    score = _round_score(focus_ratio * 10.0)
    flags = []
    if focus_ratio >= 0.60:
        flags.append("host_focused")
    if phone_ratio >= 0.20:
        flags.append("host_looking_at_phone")
    if other_ratio >= 0.20:
        flags.append("host_doing_other")
    if unknown_ratio >= 0.20:
        flags.append("host_focus_uncertain")
    if outlier_dropped is not None:
        flags.append("host_focus_outlier_dropped")

    debug_paths = {}
    if bool(getattr(cfg, "SCORER_VISION_DEBUG", False)):
        try:
            debug_paths = _write_host_focus_debug_artifacts(
                clip_path,
                str(clip_id or clip_path.stem),
                debug_frames,
            )
        except Exception as exc:
            log.warning("Could not write host focus debug for %s: %s", clip_path, exc)
            debug_paths = {"error": str(exc)}

    return {
        "score": score,
        "flags": _dedupe_flags(flags),
        "metrics": {
            "enabled": True,
            "model": model_name,
            "base_url": getattr(cfg, "SCORER_VISION_BASE_URL", "http://localhost:1235/v1"),
            "sampled_frames": sampled,
            "scored_frames": len(scoring_samples),
            "total_frames": total_frames,
            "fps": round(fps, 3) if fps > 0 else None,
            "frame_sample_rate": frame_sample_rate,
            "skip_first_frame": skip_first_frame,
            "skipped_initial_frame": skipped_initial_frame,
            "encoded_frame_size": "512x512",
            "counts": counts,
            "scoring_counts": scoring_counts,
            "ratios": {
                "A": round(focus_ratio, 4),
                "B": round(phone_ratio, 4),
                "C": round(other_ratio, 4),
                "unknown": round(unknown_ratio, 4),
            },
            "drop_outliers_enabled": bool(getattr(cfg, "SCORER_FOCUS_DROP_OUTLIERS", True)),
            "samples": samples,
            "debug": debug_paths,
        },
    }


def _host_focus_label_confidence(label: str, raw: str) -> float:
    clean = str(raw or "").strip().upper()
    if label not in {"A", "B", "C"}:
        return 0.0
    if re.fullmatch(r"[ABC]", clean):
        return 1.0
    if re.search(r"\b[ABC]\b", clean):
        return 0.75
    return 0.5


def _select_host_focus_outlier(samples: list[dict[str, Any]]) -> int | None:
    if len(samples) < 6:
        return None
    labels = [str(sample.get("label") or "unknown") for sample in samples]
    label_counts = Counter(labels)
    majority_label, majority_count = label_counts.most_common(1)[0]

    candidates = []
    for idx, sample in enumerate(samples):
        label = str(sample.get("label") or "unknown")
        confidence = _safe_float(sample.get("confidence"), default=0.0)
        is_minority = label != majority_label
        candidates.append(
            (
                confidence,
                0 if is_minority and majority_count >= len(samples) - 1 else 1,
                0 if label == "unknown" else 1,
                idx,
            )
        )
    return sorted(candidates)[0][3]


def _write_host_focus_debug_artifacts(
    clip_path: Path,
    clip_id: str,
    debug_frames: list[dict[str, Any]],
) -> dict[str, str]:
    if not debug_frames:
        return {}
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("opencv-python and numpy are required for focus debug output") from exc

    output_dir = clip_path.parent
    safe_id = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(clip_id or clip_path.stem)).strip("_")
    image_path = output_dir / f"{safe_id}_focus_debug.jpg"
    json_path = output_dir / f"{safe_id}_focus_debug.json"
    label_colors = {
        "A": (46, 204, 113),
        "B": (39, 39, 220),
        "C": (0, 165, 255),
        "unknown": (0, 165, 255),
    }
    thumbs = []
    breakdown = []
    for idx, item in enumerate(debug_frames):
        sample = item["sample"]
        label = str(sample.get("label") or "unknown")
        frame = item["frame"]
        thumb = cv2.resize(frame, (256, 256), interpolation=cv2.INTER_AREA)
        color = label_colors.get(label, label_colors["unknown"])
        label_text = label if label in {"A", "B", "C"} else "?"
        if sample.get("outlier_dropped"):
            label_text += " DROP"
        cv2.rectangle(thumb, (0, 0), (96 if "DROP" in label_text else 42, 34), color, -1)
        cv2.putText(
            thumb,
            label_text,
            (8, 24),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )
        thumbnail_path = output_dir / f"{safe_id}_focus_frame_{idx:02d}.jpg"
        cv2.imwrite(str(thumbnail_path), thumb, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
        thumbs.append(thumb)
        breakdown.append(
            {
                "frame_index": int(sample.get("frame") or 0),
                "timestamp_seconds": _safe_float(sample.get("time"), default=0.0),
                "label": label_text,
                "raw_response": str(sample.get("raw") or ""),
                "thumbnail_path": str(thumbnail_path.resolve()),
                "outlier_dropped": bool(sample.get("outlier_dropped", False)),
                "confidence": _safe_float(sample.get("confidence"), default=0.0),
            }
        )

    columns = min(4, len(thumbs))
    rows = math.ceil(len(thumbs) / columns)
    blank = np.zeros((256, 256, 3), dtype=thumbs[0].dtype)
    grid_rows = []
    for row_idx in range(rows):
        row_thumbs = thumbs[row_idx * columns : (row_idx + 1) * columns]
        while len(row_thumbs) < columns:
            row_thumbs.append(blank.copy())
        grid_rows.append(np.hstack(row_thumbs))
    sheet = np.vstack(grid_rows)
    cv2.imwrite(str(image_path), sheet, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
    _write_json_atomic(json_path, breakdown)
    return {
        "contact_sheet_path": str(image_path.resolve()),
        "breakdown_path": str(json_path.resolve()),
    }


def _classify_host_focus_frame(client, model_name: str, image_base64: str, timeout: float) -> tuple[str, str]:
    prompt = (
        "Lihat gambar ini. Kategorikan aktivitas host:\n"
        "(A) Host berbicara dan terlibat dengan siaran — termasuk melihat layar chat, "
        "menghadap kamera, atau berbicara sambil melihat ke arah manapun yang wajar "
        "untuk konteks livestream\n"
        "(B) Host melihat ke bawah ke ponsel atau perangkat pribadi yang dipegang di tangan\n"
        "(C) Host tidak memperhatikan siaran — sedang dandan, merapikan rambut, "
        "berbicara dengan orang lain di luar kamera, atau membelakangi kamera\n"
        "Jawab hanya dengan satu huruf: A, B, atau C"
    )
    response = client.chat.completions.create(
        model=model_name,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{image_base64}",
                        },
                    },
                ],
            }
        ],
        temperature=0.0,
        max_tokens=4,
        timeout=timeout,
    )
    raw = (response.choices[0].message.content or "").strip().upper()
    match = re.search(r"\b([ABC])\b", raw)
    if not match:
        compact = re.sub(r"[^ABC]", "", raw)
        if compact:
            return compact[0], raw
        return "unknown", raw
    return match.group(1), raw


def _encode_frame_as_jpeg_base64(cv2_module, frame) -> str:
    resized = cv2_module.resize(frame, (512, 512), interpolation=cv2_module.INTER_AREA)
    ok, buffer = cv2_module.imencode(".jpg", resized, [int(cv2_module.IMWRITE_JPEG_QUALITY), 82])
    if not ok:
        raise RuntimeError("could not encode video frame as JPEG")
    return base64.b64encode(buffer.tobytes()).decode("ascii")


def _score_quality(clip_path: Path, cfg) -> tuple[float | None, list[str], dict[str, Any]]:
    duration = _probe_duration(clip_path)
    loudness_lufs, loudness_error, silence_metrics, silence_error = _probe_audio_quality(clip_path, duration)

    flags: list[str] = []
    parts: dict[str, tuple[float, float]] = {}

    metrics: dict[str, Any] = {
        "duration_seconds": duration,
        "integrated_lufs": loudness_lufs,
        "silence": silence_metrics,
    }
    if loudness_error:
        metrics["loudness_error"] = loudness_error
        flags.append("loudness_unavailable")
    if silence_error:
        metrics["silence_error"] = silence_error
        flags.append("silence_probe_unavailable")

    if duration is not None:
        min_duration = float(getattr(cfg, "MIN_CLIP_DURATION", 20.0) or 20.0)
        max_duration = float(getattr(cfg, "MAX_CLIP_DURATION", 60.0) or 60.0)
        duration_score = _duration_score(duration, min_duration, max_duration)
        parts["duration"] = (duration_score, 0.35)
        if duration < min_duration:
            flags.append("short_duration")
        elif duration > max_duration:
            flags.append("long_duration")

    if loudness_lufs is not None and math.isfinite(loudness_lufs):
        loudness_score = max(0.0, 10.0 - abs(loudness_lufs - (-16.0)) * 0.85)
        parts["loudness"] = (loudness_score, 0.35)
        if loudness_lufs < -25.0:
            flags.append("audio_too_quiet")
        elif loudness_lufs > -8.0:
            flags.append("audio_too_loud")
        else:
            flags.append("audio_level_ok")

    if silence_metrics:
        max_silence = float(silence_metrics.get("max_silence_seconds") or 0.0)
        silence_fraction = float(silence_metrics.get("silence_fraction") or 0.0)
        silence_score = max(0.0, 10.0 - max(0.0, max_silence - 1.0) * 1.7 - silence_fraction * 18.0)
        parts["silence"] = (silence_score, 0.30)
        if max_silence >= 2.5 or silence_fraction >= 0.20:
            flags.append("long_silence")
        elif silence_fraction <= 0.05:
            flags.append("low_silence")

    if not parts:
        return None, flags or ["quality_unavailable"], metrics

    weighted = sum(score * weight for score, weight in parts.values())
    weight_total = sum(weight for _, weight in parts.values())
    return _round_score(weighted / weight_total), _dedupe_flags(flags), metrics


def _content_signal_counts(transcript_text: str, product_name: str, cfg) -> dict[str, int]:
    text = transcript_text.lower()
    promo_matches = _count_pattern_matches(
        text,
        [
            r"\brp\.?\s*\d+",
            r"\bharga\w*\b",
            r"\bdiskon\b",
            r"\bpromo\b",
            r"\bvoucher\b",
            r"\bgratis\s*ongkir\b",
            r"\bongkir\b",
            r"\betalase\b",
            r"\bcheckout\b",
            r"\bcheck\s*out\b",
            r"\b\d+\s*%",
            r"\b\d+\s*(?:rb|ribu|k)\b",
        ],
    )
    demo_matches = _count_pattern_matches(
        text,
        [
            r"\bcoba\w*\b",
            r"\bpakai\w*\b",
            r"\bpake\w*\b",
            r"\bdipakai\b",
            r"\baplikasi\w*\b",
            r"\baplikasikan\b",
            r"\bapply\b",
            r"\boles\w*\b",
            r"\bsemprot\w*\b",
            r"\bspray\b",
            r"\bbilas\b",
            r"\btekstur\b",
            r"\bstep\b",
        ],
    )
    benefit_matches = _count_pattern_matches(
        text,
        [
            r"\bcerah\w*\b",
            r"\bmencerah\w*\b",
            r"\bglow\w*\b",
            r"\blemb[ae]p\w*\b",
            r"\bjerawat\b",
            r"\bflek\b",
            r"\bnoda\b",
            r"\bkusam\b",
            r"\bhalus\b",
            r"\bbersih\w*\b",
            r"\bpori\w*\b",
            r"\bberuntus\w*\b",
            r"\bkemerahan\b",
            r"\bmemudar\w*\b",
            r"\bbekas\b",
        ],
    )
    ingredient_matches = _count_pattern_matches(
        text,
        [
            r"\bvitamin\s*c\b",
            r"\bniacinamide\b",
            r"\btranexamic\b",
            r"\barbutin\b",
            r"\bhyaluronic\b",
            r"\baha\b",
            r"\bbha\b",
            r"\bretinol\b",
            r"\bceramide\b",
            r"\bsalicylic\b",
            r"\bglutathione\b",
        ],
    )
    product_mentions = _count_product_mentions(text, product_name, cfg)
    return {
        "promo_matches": promo_matches,
        "product_mentions": product_mentions,
        "demo_matches": demo_matches,
        "benefit_matches": benefit_matches,
        "ingredient_matches": ingredient_matches,
    }


def _focus_flags_from_counts(counts: dict[str, int], include_product_focus: bool = True) -> list[str]:
    flags = []
    if int(counts.get("promo_matches", 0)) > 0:
        flags.append("promo_focus")
    if int(counts.get("demo_matches", 0)) > 0:
        flags.append("demo_focus")
    if int(counts.get("benefit_matches", 0)) > 0:
        flags.append("benefit_focus")
    if int(counts.get("ingredient_matches", 0)) > 0:
        flags.append("ingredient_focus")
    if include_product_focus and int(counts.get("product_mentions", 0)) > 0 and not flags:
        flags.append("product_focus")
    return flags


def _normalize_content_focus_flags(raw_flags: list[Any], counts: dict[str, int], score: float) -> list[str]:
    allowed_focus = {"promo_focus", "demo_focus", "benefit_focus", "ingredient_focus", "product_focus"}
    allowed_quality = {"off_topic", "filler_heavy", "product_ambiguous", "promo_price_only"}
    mapped_flags = {
        "usage_instruction": "demo_focus",
        "demo_signal": "demo_focus",
        "price_mentioned": "promo_focus",
        "promo_mentioned": "promo_focus",
        "benefit_claim": "benefit_focus",
        "ingredient_mention": "ingredient_focus",
        "ingredient_focus": "ingredient_focus",
        "product_irrelevant": "product_ambiguous",
        "not_product_relevant": "product_ambiguous",
    }

    flags: list[str] = []
    qwen_focus_flags: list[str] = []
    for raw_flag in raw_flags:
        clean = _clean_flag(raw_flag)
        clean = mapped_flags.get(clean, clean)
        if clean in allowed_focus:
            qwen_focus_flags.append(clean)
        elif clean in allowed_quality:
            flags.append(clean)

    count_focus_flags = _focus_flags_from_counts(counts)
    for flag in count_focus_flags:
        flags.append(flag)
    if not count_focus_flags:
        flags.extend(qwen_focus_flags)

    if score < 4:
        flags.append("off_topic")
    if not _focus_flags_from_counts(counts) and int(counts.get("product_mentions", 0)) <= 0 and score < 6:
        flags.append("off_topic")
    return _dedupe_flags(flags)


def _apply_promo_price_only_content_penalty(
    score: float,
    flags: list[str],
    counts: dict[str, int],
) -> tuple[float, list[str], bool]:
    promo_only = (
        int(counts.get("promo_matches", 0)) > 0
        and int(counts.get("demo_matches", 0)) <= 0
        and int(counts.get("benefit_matches", 0)) <= 0
        and int(counts.get("ingredient_matches", 0)) <= 0
    )
    if not promo_only:
        return score, _dedupe_flags(flags), False
    capped_score = min(float(score), 4.0)
    capped_flags = _dedupe_flags(list(flags) + ["promo_focus", "promo_price_only"])
    return _round_score(capped_score), capped_flags, capped_score < float(score)


def _score_engagement(transcript_text: str, product_name: str, cfg) -> tuple[float, list[str], dict[str, Any]]:
    counts = _content_signal_counts(transcript_text, product_name, cfg)
    price_matches = counts["promo_matches"]
    demo_matches = counts["demo_matches"]
    benefit_matches = counts["benefit_matches"]
    ingredient_matches = counts["ingredient_matches"]
    product_mentions = counts["product_mentions"]

    flags = _focus_flags_from_counts(counts)
    if not flags and not product_mentions:
        flags.append("off_topic")

    score = min(10.0, 0.0)
    score += min(2.5, price_matches * 1.25)
    score += 2.0 if product_mentions else 0.0
    score += min(2.5, demo_matches * 0.75)
    score += min(3.0, benefit_matches * 0.60)
    score += min(1.0, ingredient_matches * 0.25)

    return _round_score(score), _dedupe_flags(flags), {
        "price_matches": price_matches,
        "promo_matches": price_matches,
        "product_mentions": product_mentions,
        "demo_matches": demo_matches,
        "benefit_matches": benefit_matches,
        "ingredient_matches": ingredient_matches,
    }


def _probe_duration(clip_path: Path) -> float | None:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(clip_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout or "{}")
        return float(payload.get("format", {}).get("duration"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _probe_audio_quality(
    clip_path: Path,
    duration: float | None,
) -> tuple[float | None, str | None, dict[str, Any] | None, str | None]:
    timeout = max(30, min(240, int((duration or 30) * 3)))
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(clip_path),
            "-filter_complex",
            (
                "[0:a]asplit=2[a_loud][a_sil];"
                "[a_loud]loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json[loud];"
                "[a_sil]silencedetect=noise=-35dB:d=1.0[sil]"
            ),
            "-map",
            "[loud]",
            "-map",
            "[sil]",
            "-f",
            "null",
            "-",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    output = result.stderr or result.stdout or ""
    if result.returncode != 0:
        error = output[-500:] or "ffmpeg audio quality probe failed"
        return None, error, None, error

    loudness_lufs = None
    loudness_error = None
    matches = re.findall(r"\{[\s\S]*?\"input_i\"[\s\S]*?\}", output)
    if matches:
        try:
            payload = json.loads(matches[-1])
            loudness_lufs = float(payload.get("input_i"))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            loudness_error = str(exc)
    else:
        loudness_error = "loudnorm JSON not found"

    durations = [float(item) for item in re.findall(r"silence_duration:\s*([0-9.]+)", output)]
    total_silence = sum(durations)
    max_silence = max(durations) if durations else 0.0
    silence_fraction = (total_silence / duration) if duration and duration > 0 else 0.0
    silence_metrics = {
        "events": len(durations),
        "total_silence_seconds": round(total_silence, 3),
        "max_silence_seconds": round(max_silence, 3),
        "silence_fraction": round(silence_fraction, 4),
    }

    return loudness_lufs, loudness_error, silence_metrics, None


def _read_transcript_path(path: Path) -> str:
    if path.suffix.lower() == ".json":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return ""
        return transcript_to_text(payload)
    try:
        return " ".join(path.read_text(encoding="utf-8").split())
    except Exception:
        return ""


def _words_to_text(words: list) -> str:
    tokens = []
    for item in words:
        if isinstance(item, dict):
            token = str(item.get("word", "")).strip()
        else:
            token = str(item).strip()
        if token:
            tokens.append(token)
    return " ".join(tokens)


def _count_pattern_matches(text: str, patterns: list[str]) -> int:
    return sum(len(re.findall(pattern, text, flags=re.IGNORECASE)) for pattern in patterns)


def _count_product_mentions(text: str, product_name: str, cfg) -> int:
    terms = [product_name, getattr(cfg, "BRAND_NAME", ""), "PROYA"]
    terms.extend(str(value) for value in (getattr(cfg, "PRODUCT_CLASSES", {}) or {}).values())
    count = 0
    for term in terms:
        normalized = str(term or "").strip()
        if not normalized or normalized.casefold() == "general":
            continue
        pattern = r"\b" + re.escape(normalized.lower()) + r"\b"
        count += len(re.findall(pattern, text, flags=re.IGNORECASE))
    return count


def _infer_working_dir(output_dir: Path, cfg) -> Path:
    return Path(getattr(cfg, "WORKING_DIR", "working")) / output_dir.name


def _load_transcript_words(working_dir: Path) -> list[dict[str, Any]]:
    transcript_path = working_dir / "transcript.json"
    if not transcript_path.exists():
        log.warning("Transcript not found for batch scoring: %s", transcript_path)
        return []
    try:
        payload = json.loads(transcript_path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("Could not read transcript for batch scoring: %s", exc)
        return []
    words = payload.get("words", []) if isinstance(payload, dict) else []
    return [word for word in words if isinstance(word, dict)]


def _build_transcript_word_index(words: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(
        words or [],
        key=lambda word: (
            _safe_float(word.get("start"), default=0.0),
            _safe_float(word.get("end"), default=0.0),
        ),
    )
    return {
        "words": ordered,
        "starts": [_safe_float(word.get("start"), default=0.0) for word in ordered],
    }


def _get_clip_words_from_index(index: dict[str, Any], clip_start: Any, clip_end: Any) -> list[dict[str, Any]]:
    start = _safe_float(clip_start, default=0.0)
    end = _safe_float(clip_end, default=start)
    words = index.get("words", [])
    starts = index.get("starts", [])
    left = bisect_left(starts, start)
    right = bisect_right(starts, end + 0.5)
    clip_words = []
    for word in words[left:right]:
        word_start = _safe_float(word.get("start"), default=0.0)
        word_end = _safe_float(word.get("end"), default=word_start)
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


def _attach_score_to_manifest_row(row: dict[str, Any], score: dict[str, Any], cfg) -> None:
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


def _duration_score(duration: float, min_duration: float, max_duration: float) -> float:
    if min_duration <= duration <= max_duration:
        return 10.0
    if duration < min_duration:
        return max(0.0, 10.0 - ((min_duration - duration) / max(min_duration, 1.0)) * 8.0)
    return max(0.0, 10.0 - ((duration - max_duration) / max(max_duration, 1.0)) * 8.0)


def _normalized_dimension_weights(raw_weights: Any) -> dict[str, float]:
    defaults = {"content": 0.466667, "quality": 0.2, "engagement": 0.333333}
    if isinstance(raw_weights, dict):
        for key in defaults:
            if key in raw_weights:
                defaults[key] = max(0.0, float(raw_weights[key]))
    total = sum(defaults.values())
    if total <= 0:
        return {"content": 0.466667, "quality": 0.2, "engagement": 0.333333}
    return {key: round(value / total, 6) for key, value in defaults.items()}


def _effective_dimension_weights(cfg, host_focus_score: float | None) -> dict[str, float]:
    weights = _normalized_dimension_weights(getattr(cfg, "SCORER_WEIGHTS", None))
    host_weight = float(getattr(cfg, "SCORER_HOST_FOCUS_WEIGHT", 0.0) or 0.0)
    if bool(getattr(cfg, "SCORER_VISION_ENABLED", False)) and host_focus_score is not None:
        host_weight = 0.20
    host_weight = max(0.0, min(1.0, host_weight))
    if host_weight > 0.0 and host_focus_score is not None:
        scale = 1.0 - host_weight
        weights = {key: round(value * scale, 6) for key, value in weights.items()}
        weights["host_focus"] = round(host_weight, 6)
    total = sum(weights.values())
    if total <= 0:
        return _normalized_dimension_weights(None)
    return {key: round(value / total, 6) for key, value in weights.items()}


def _weighted_total(scores: dict[str, float | None], weights: dict[str, float]) -> float:
    available = [
        (key, score)
        for key, score in scores.items()
        if score is not None and math.isfinite(float(score))
    ]
    if not available:
        return 0.0
    total_weight = sum(weights.get(key, 0.0) for key, _ in available)
    if total_weight <= 0:
        return 0.0
    return sum(float(score) * weights.get(key, 0.0) for key, score in available) / total_weight


def _apply_score_caps(
    total_score: float,
    flags: list[str],
    host_focus_score: float | None,
    cfg,
) -> tuple[float, list[str]]:
    if not bool(getattr(cfg, "SCORER_APPLY_CAPS", True)):
        return total_score, []

    clean_flags = {_clean_flag(flag) for flag in flags}
    host_not_focused = (
        "host_not_focused" in clean_flags
        or (
            host_focus_score is not None
            and _safe_float(host_focus_score, default=10.0) < 4.0
        )
    )
    cap_rules = [
        ("off_topic", "off_topic" in clean_flags, 5.0),
        ("product_not_visible", "product_not_visible" in clean_flags, 6.5),
        ("no_transcript", "no_transcript" in clean_flags, 4.0),
        ("host_not_focused", host_not_focused, 6.0),
        (
            "off_topic_and_host_not_focused",
            "off_topic" in clean_flags and host_not_focused,
            3.0,
        ),
    ]

    capped = float(total_score)
    applied = []
    for name, condition, cap in cap_rules:
        if condition and capped > cap:
            capped = cap
            applied.append(name)
    return capped, applied


def _round_optional_score(value: float | None) -> float | None:
    if value is None:
        return None
    return _round_score(value)


def _round_score(value: float) -> float:
    if value is None or not math.isfinite(float(value)):
        return 0.0
    return round(max(0.0, min(10.0, float(value))), 2)


def _format_optional_score(value: Any) -> str:
    numeric = _round_optional_score(_safe_float(value, default=float("nan")) if value is not None else None)
    return "-" if numeric is None else f"{numeric:.2f}"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clean_flag(value: Any) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", str(value or "").strip().lower()).strip("_")


def _dedupe_flags(flags: list[str]) -> list[str]:
    seen = set()
    output = []
    for flag in flags:
        clean = _clean_flag(flag)
        if clean and clean not in seen:
            output.append(clean)
            seen.add(clean)
    return output


def _fallback_summary(scores: dict[str, float | None], flags: list[str]) -> str:
    available = {
        key: value
        for key, value in scores.items()
        if value is not None and math.isfinite(float(value))
    }
    if not available:
        return "Skor otomatis terbatas karena beberapa pemeriksaan tidak tersedia."
    best_key = max(available, key=lambda item: float(available[item]))
    low_flags = [flag for flag in flags if flag in {"off_topic", "product_not_visible", "long_silence"}]
    if low_flags:
        return f"Klip tertahan oleh {low_flags[0].replace('_', ' ')}, meski dimensi {best_key} masih membantu."
    return f"Klip mendapat skor terutama dari dimensi {best_key} yang paling kuat."


def write_scores_report(scores: list[dict[str, Any]], output_dir: str | Path, cfg=None) -> Path:
    root = Path(output_dir)
    report_path = root / "scores_report.txt"
    export_threshold = float(getattr(cfg, "SCORER_EXPORT_READY_THRESHOLD", 7.0) or 7.0)
    review_threshold = float(getattr(cfg, "SCORER_REVIEW_THRESHOLD", 5.0) or 5.0)
    products = ["Cleanser", "Serum", "Toner", "Eye Cream", "Sheet Mask", "Moisturizer"]
    product_scores: dict[str, list[float]] = {product: [] for product in products}
    dimension_scores: dict[str, list[float]] = {
        "content": [],
        "quality": [],
        "engagement": [],
    }
    tier_counts = {"export_ready": 0, "review_needed": 0, "rejected": 0}
    flag_counter: Counter[str] = Counter()

    for score in scores:
        if not isinstance(score, dict):
            continue
        total = score.get("total_score")
        if total is not None:
            bucket = _canonical_report_product(score.get("product"))
            if bucket in product_scores:
                product_scores[bucket].append(_safe_float(total, default=0.0))
            tier_counts[_score_tier(total, export_threshold, review_threshold)] += 1
        for dimension in dimension_scores:
            value = score.get(f"{dimension}_score")
            if value is not None:
                dimension_scores[dimension].append(_safe_float(value, default=0.0))
        flag_counter.update(_clean_flag(flag) for flag in score.get("flags", []) if _clean_flag(flag))

    top_scores = sorted(
        (score for score in scores if isinstance(score, dict)),
        key=lambda item: _safe_float(item.get("total_score"), default=-1.0),
        reverse=True,
    )[:3]

    lines = [
        "PROYA Clip Score Trend Report",
        f"Generated: {datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')}",
        f"Output: {root.resolve()}",
        "",
        "Average Total Score by Product",
    ]
    for product in products:
        values = product_scores[product]
        lines.append(f"- {product}: {_format_average(values)} ({len(values)} clips)")

    lines.extend(["", "Average Score by Dimension"])
    for dimension in ["content", "quality", "engagement"]:
        values = dimension_scores[dimension]
        lines.append(f"- {dimension.title()}: {_format_average(values)}")

    lines.extend(["", "Top 3 Clips Overall"])
    if top_scores:
        for index, score in enumerate(top_scores, start=1):
            lines.append(
                f"{index}. {score.get('clip_id', '-')} "
                f"({ _format_optional_score(score.get('total_score')) }) - "
                f"{str(score.get('summary') or '').strip()}"
            )
    else:
        lines.append("- No scored clips found.")

    lines.extend(["", "Most Common Flags"])
    if flag_counter:
        for flag, count in flag_counter.most_common(10):
            lines.append(f"- {flag}: {count}")
    else:
        lines.append("- No flags found.")

    lines.extend(
        [
            "",
            "Tier Counts",
            f"- export_ready (>= {export_threshold:.1f}): {tier_counts['export_ready']}",
            f"- review_needed (>= {review_threshold:.1f} and < {export_threshold:.1f}): {tier_counts['review_needed']}",
            f"- rejected (< {review_threshold:.1f}): {tier_counts['rejected']}",
        ]
    )
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


def _move_scored_clips_by_tier(scores: list[dict[str, Any]], output_dir: Path, cfg) -> dict[str, Any]:
    enabled = bool(getattr(cfg, "SCORER_AUTO_SORT_ENABLED", False)) if cfg is not None else False
    stats = {
        "enabled": enabled,
        "mode": "move",
        "export_ready": 0,
        "review_needed": 0,
        "rejected": 0,
        "moved": 0,
        "already_sorted": 0,
        "moves": [],
        "errors": [],
    }
    if not enabled:
        return stats

    export_threshold = float(getattr(cfg, "SCORER_EXPORT_READY_THRESHOLD", 7.0) or 7.0)
    review_threshold = float(getattr(cfg, "SCORER_REVIEW_THRESHOLD", 5.0) or 5.0)
    for score in scores:
        if not isinstance(score, dict):
            continue
        source = Path(str(score.get("clip_path") or ""))
        tier = _score_tier(score.get("total_score"), export_threshold, review_threshold)
        relative = _relative_clip_output_path(score, source, output_dir)
        destination = output_dir / tier / relative
        relative_destination = _relative_to_root(destination, output_dir)
        try:
            source_exists = source.exists() and source.is_file()
            destination_exists = destination.exists() and destination.is_file()
            if not source_exists and not destination_exists:
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            same_file = False
            if source_exists and destination_exists:
                try:
                    same_file = source.resolve() == destination.resolve()
                except OSError:
                    same_file = False
            if not same_file and source_exists:
                if destination_exists:
                    destination.unlink()
                shutil.move(str(source), str(destination))
                stats["moved"] += 1
            else:
                stats["already_sorted"] += 1
            _apply_tier_move_to_score(
                score,
                destination,
                relative_destination,
                output_dir,
            )
            stats[tier] += 1
            stats["moves"].append(
                {
                    "clip_id": score.get("clip_id"),
                    "tier": tier,
                    "clip_path": str(destination.resolve()),
                    "output_file": relative_destination,
                }
            )
        except Exception as exc:
            stats["errors"].append(f"{source}: {exc}")
    return stats


def _relative_clip_output_path(score: dict[str, Any], source: Path, output_dir: Path) -> Path:
    output_file = str(score.get("output_file") or "").replace("\\", "/").strip()
    if output_file:
        path = Path(output_file)
        if path.is_absolute():
            try:
                path = path.resolve().relative_to(output_dir.resolve())
            except ValueError:
                path = Path(path.name)
        return _strip_tier_prefix(path)
    try:
        return _strip_tier_prefix(source.resolve().relative_to(output_dir.resolve()))
    except ValueError:
        return Path(source.name)


def _strip_tier_prefix(path: Path) -> Path:
    parts = list(path.parts)
    while parts and parts[0].casefold() in SCORE_TIER_DIRS:
        parts.pop(0)
    return Path(*parts) if parts else Path(path.name)


def _relative_to_root(path: Path, output_dir: Path) -> str:
    try:
        return path.resolve().relative_to(output_dir.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _apply_tier_move_to_score(
    score: dict[str, Any],
    destination: Path,
    relative_destination: str,
    output_dir: Path,
) -> None:
    score["clip_path"] = str(destination.resolve())
    score["output_file"] = relative_destination
    score["output_dir"] = str(output_dir.resolve())


def _apply_tier_moves_to_groups(
    groups: list[dict[str, Any]],
    tier_move: dict[str, Any],
    output_dir: Path,
) -> None:
    moves = {
        str(move.get("clip_id")): move
        for move in tier_move.get("moves", [])
        if isinstance(move, dict) and move.get("clip_id")
    }
    if not moves:
        return
    for group in groups:
        if not isinstance(group, dict):
            continue
        _apply_tier_move_to_group_record(group, moves, output_dir)


def _apply_tier_move_to_group_record(
    record: dict[str, Any],
    moves: dict[str, dict[str, Any]],
    output_dir: Path,
) -> None:
    clip_id = str(record.get("clip_id") or "")
    move = moves.get(clip_id)
    if move:
        record["clip_path"] = move.get("clip_path")
        record["output_file"] = move.get("output_file")
        record["output_dir"] = str(output_dir.resolve())

    representative_id = str(record.get("representative_clip_id") or "")
    representative_move = moves.get(representative_id)
    if representative_move:
        record["clip_path"] = representative_move.get("clip_path")
        record["output_file"] = representative_move.get("output_file")
        record["representative_clip_path"] = representative_move.get("clip_path")
        record["representative_output_file"] = representative_move.get("output_file")
        record["output_dir"] = str(output_dir.resolve())

    variants = record.get("variants")
    if isinstance(variants, list):
        for variant in variants:
            if isinstance(variant, dict):
                _apply_tier_move_to_group_record(variant, moves, output_dir)


def _apply_tier_move_stats_to_manifest(
    manifest: list[dict[str, Any]],
    tier_move: dict[str, Any],
) -> None:
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


def _score_tier(value: Any, export_threshold: float, review_threshold: float) -> str:
    total = _safe_float(value, default=0.0)
    if total >= export_threshold:
        return "export_ready"
    if total >= review_threshold:
        return "review_needed"
    return "rejected"


def _canonical_report_product(value: Any) -> str:
    text = str(value or "").lower()
    if "cleanser" in text or "clean" in text:
        return "Cleanser"
    if "serum" in text:
        return "Serum"
    if "toner" in text:
        return "Toner"
    if "eye" in text and "cream" in text:
        return "Eye Cream"
    if "sheet" in text or "mask" in text or "masker" in text:
        return "Sheet Mask"
    if "moist" in text or "cream" in text or "krim" in text:
        return "Moisturizer"
    return ""


def _format_average(values: list[float]) -> str:
    if not values:
        return "N/A"
    return f"{sum(values) / len(values):.2f}"


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        cleaned = value.strip().lower()
        if cleaned in {"true", "ya", "yes", "1"}:
            return True
        if cleaned in {"false", "tidak", "no", "0"}:
            return False
    return default


def _clip_mtime_ns(clip_path: Path) -> int | None:
    try:
        return clip_path.stat().st_mtime_ns
    except OSError:
        return None


def _write_json_atomic(path: Path, payload: Any) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(path)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Score rendered PROYA clips")
    parser.add_argument("clip", nargs="?", help="Path to one rendered .mp4 clip")
    parser.add_argument("transcript", nargs="?", help="Transcript text or path to transcript JSON/text")
    parser.add_argument("--product", default="general", help="Target product name")
    parser.add_argument(
        "--output-dir",
        help="Rendered output folder with manifest.json, or output root containing many manifest folders",
    )
    parser.add_argument(
        "--working-dir",
        help="Working folder with transcript.json, or working root when --output-dir is an output root",
    )
    parser.add_argument("--limit", type=int, default=None, help="Maximum clips to score")
    parser.add_argument("--include-failed", action="store_true", help="Include failed manifest rows")
    parser.add_argument("--force-rescore", action="store_true", help="Bypass scores.json cache")
    parser.add_argument(
        "--flush-every",
        type=int,
        default=None,
        help="Write partial scores_summary.json after this many scored clips",
    )
    parser.add_argument(
        "--enable-vision",
        action="store_true",
        help="Enable optional Qwen2.5-VL host_focus_score for this run",
    )
    parser.add_argument(
        "--vision-base-url",
        help="OpenAI-compatible LM Studio endpoint for the vision model, e.g. http://localhost:1235/v1",
    )
    parser.add_argument(
        "--vision-model",
        help="Vision model name loaded in LM Studio, e.g. qwen2.5-vl-32b-instruct",
    )
    parser.add_argument(
        "--vision-timeout",
        type=float,
        help="Timeout in seconds for each vision-model frame request",
    )
    args = parser.parse_args()

    import config as cfg

    if args.force_rescore:
        cfg.SCORER_FORCE_RESCORE = True
    if args.enable_vision:
        cfg.SCORER_VISION_ENABLED = True
    if args.vision_base_url:
        cfg.SCORER_VISION_BASE_URL = args.vision_base_url
    if args.vision_model:
        cfg.SCORER_VISION_MODEL = args.vision_model
    if args.vision_timeout is not None:
        cfg.SCORER_VISION_TIMEOUT = args.vision_timeout

    if args.output_dir:
        scores = score_output_tree(
            args.output_dir,
            working_root=args.working_dir,
            cfg=cfg,
            limit=args.limit,
            include_failed=args.include_failed,
            flush_every=args.flush_every,
        )
        print(f"\nScored {len(scores)} clip(s)")
    else:
        if not args.clip or not args.transcript:
            parser.error("provide clip and transcript, or use --output-dir")
        result = score_clip(args.clip, args.transcript, args.product, cfg=cfg)
        print(json.dumps(result, ensure_ascii=False, indent=2))
