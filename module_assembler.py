from __future__ import annotations

import hashlib
import copy
import json
import logging
import os
import re
import subprocess
import time
from contextlib import contextmanager
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from module_extractor import (
    QUALITY_APPROVED,
    QUALITY_NO_VISUAL_EVENTS,
    PRODUCT_FOLDERS,
    ROLE_FOLDERS,
    canonical_product,
    library_index_lock,
    module_sidecar_path,
    module_quality_fields,
    probe_media,
    read_library_index,
    rebuild_library_index,
    source_date_from_source_video,
)

log = logging.getLogger("proya.module_assembler")

ASSEMBLY_SCHEMA_VERSION = 1
SENTENCE_END_RE = re.compile(r"[.!?]+[\"')\]]*$")
RISKY_HOOK_RE = re.compile(
    r"\b(?:terbaik|nomor\s*1|no\.?\s*1|paling\s+(?:ampuh|cepat|bagus|kuat)|100\s*%|"
    r"menyembuh\w*|menghilang\w*|hilang\w*|hapus\w*|memutihkan|pasti|dijamin|jamin|"
    r"permanen|instan|bebas\s+jerawat|tanpa\s+efek\s+samping|dalam\s+\d+\s+(?:hari|minggu)|"
    r"flek\s+hilang)\b",
    re.IGNORECASE,
)

SAFE_HOOKS = {
    "cleanser": "Wajah Ketarik Pas Cuci Muka?",
    "toner": "Kulit Terasa Kering Setelah Cuci Muka?",
    "serum": "Kulit Kusam? Cek Step Ini",
    "eye_cream": "Area Mata Terlihat Lelah?",
    "mask": "Butuh Step Perawatan Cepat?",
    "skin_cream": "Kulit Kering? Cek Pelembap Ini",
}


def build_modular_assembly_jobs(index: dict[str, Any], output_dir: str | Path, cfg) -> list[dict[str, Any]]:
    """Build ranked hook+main+cta assembly jobs from a module index."""
    modules = _load_index_modules_with_words(index, cfg)
    same_date_only = bool(getattr(cfg, "MODULE_ASSEMBLY_SAME_DATE_ONLY", True))
    source_date_filter = _normalize_source_date_value(
        getattr(cfg, "MODULE_ASSEMBLY_SOURCE_DATE", "")
    )
    grouped: dict[str, dict[str, list[dict[str, Any]]]] = {
        product: {role: [] for role in ROLE_FOLDERS}
        for product in PRODUCT_FOLDERS
    }
    for module in modules:
        source_date = _module_source_date(module, warn=same_date_only)
        if source_date_filter and source_date != source_date_filter:
            continue
        if source_date:
            module["source_date"] = source_date
        elif same_date_only:
            continue
        product = module.get("product")
        role = module.get("role")
        if product in grouped and role in grouped[product] and _module_allowed_for_assembly(module, cfg):
            grouped[product][role].append(module)

    for product_roles in grouped.values():
        for role in ROLE_FOLDERS:
            product_roles[role].sort(key=_module_rank, reverse=True)

    candidates = []
    for product, roles in grouped.items():
        if not _product_ready_for_assembly(roles, cfg):
            continue
        if same_date_only:
            candidates.extend(_same_date_assembly_candidates(product, roles, output_dir, cfg))
            continue
        for main in roles["main"]:
            candidates.extend(_assembly_candidates_for_main(product, main, roles, output_dir, cfg))

    candidates.sort(key=lambda job: job.get("rank_score", 0.0), reverse=True)
    for rank, job in enumerate(candidates, start=1):
        job["candidate_rank"] = rank
    return _candidate_pool(candidates, cfg)


def render_modular_assemblies(
    jobs: list[dict[str, Any]],
    cfg,
    output_dir: str | Path | None = None,
    working_dir: str | Path | None = None,
    progress_callback=None,
) -> dict[str, Any]:
    """Render prebuilt modular assembly jobs through edit_clip, compliance, and scorer."""
    if not jobs:
        return {
            "jobs": 0,
            "created": 0,
            "skipped": 0,
            "failed": 0,
            "blocked": 0,
            "pool_examined": 0,
            "products_created": 0,
            "manifest": [],
        }

    output_root = Path(output_dir or jobs[0].get("output_dir") or ".")
    output_subdir = _assembly_output_subdir(cfg)
    modular_dir = output_root / output_subdir if output_subdir else output_root
    modular_dir.mkdir(parents=True, exist_ok=True)
    raw_root = Path(working_dir or output_root) / "modular_assemblies"
    raw_root.mkdir(parents=True, exist_ok=True)
    manifest_path = modular_dir / "manifest.json"

    with modular_output_lock(modular_dir, cfg):
        return _render_modular_assemblies_locked(
            jobs=jobs,
            cfg=cfg,
            modular_dir=modular_dir,
            raw_root=raw_root,
            manifest_path=manifest_path,
            progress_callback=progress_callback,
        )


def _render_modular_assemblies_locked(
    jobs: list[dict[str, Any]],
    cfg,
    modular_dir: Path,
    raw_root: Path,
    manifest_path: Path,
    progress_callback=None,
) -> dict[str, Any]:
    manifest: list[dict[str, Any]] = []
    render_jobs = _expand_modular_jobs_with_variants(jobs, cfg)

    output_subdir = _assembly_output_subdir(cfg)
    render_limit = _cfg_nonnegative_int(cfg, "MODULE_ASSEMBLY_RENDER_LIMIT", 3)
    max_per_product = _cfg_nonnegative_int(cfg, "MODULE_ASSEMBLY_MAX_PER_PRODUCT", 1)
    compliance_prefilter = bool(getattr(cfg, "MODULE_ASSEMBLY_COMPLIANCE_PREFILTER", True))
    created = skipped = failed = blocked = pool_examined = 0
    created_by_product: dict[str, int] = defaultdict(int)
    created_base_by_product: dict[str, set[str]] = defaultdict(set)
    selected_base_ids: set[str] = set()
    created_base_ids: set[str] = set()
    skipped_base_ids: set[str] = set()
    if render_limit <= 0:
        _write_json_atomic(manifest_path, manifest)
        return {
            "jobs": len(jobs),
            "render_jobs": len(render_jobs),
            "created": 0,
            "skipped": 0,
            "failed": 0,
            "blocked": 0,
            "pool_examined": 0,
            "products_created": 0,
            "manifest_path": str(manifest_path.resolve()),
            "manifest": manifest,
            "scores": [],
        }

    for index, job in enumerate(render_jobs, start=1):
        base_clip_id = str(job.get("base_clip_id") or job.get("clip_id") or "")
        if base_clip_id in skipped_base_ids:
            continue
        product = str(job.get("product") or "")
        if base_clip_id not in selected_base_ids:
            if len(created_base_ids) >= render_limit:
                break
            if max_per_product and len(created_base_by_product[product]) >= max_per_product:
                skipped += 1
                skipped_base_ids.add(base_clip_id)
                continue
            selected_base_ids.add(base_clip_id)
            pool_examined += 1
        job["raw_path"] = str(raw_root / f"{job['clip_id']}_raw.mp4")
        output_relative_path = _job_modular_output_relative_path(job)
        job["output_path"] = str(modular_dir / output_relative_path)
        job["output_file"] = _assembly_relative_output_file(output_relative_path, cfg)
        job["version_dir"] = str(job.get("version_dir") or output_subdir)
        _prepare_job_product_events(job, cfg)

        if Path(job["output_path"]).exists():
            if _existing_modular_output_valid(job):
                compliance_result = _apply_compliance(job, cfg) if compliance_prefilter else None
                if compliance_result and compliance_result.get("blocked"):
                    row = _manifest_row(job, "compliance_blocked")
                    manifest.append(row)
                    blocked += 1
                    skipped_base_ids.add(base_clip_id)
                    _write_json_atomic(manifest_path, manifest)
                    continue
                row = _manifest_row(job, "skipped")
                manifest.append(row)
                skipped += 1
                created += 1
                created_base_ids.add(base_clip_id)
                created_base_by_product[product].add(base_clip_id)
                created_by_product[product] += 1
                _write_json_atomic(manifest_path, manifest)
                continue
            try:
                Path(job["output_path"]).unlink()
            except OSError as exc:
                row = _manifest_row(job, "failed_existing_invalid")
                row["error"] = f"existing output invalid and could not be removed: {exc}"
                manifest.append(row)
                failed += 1
                _write_json_atomic(manifest_path, manifest)
                continue

        try:
            compliance_result = _apply_compliance(job, cfg) if compliance_prefilter else None
            if compliance_result and compliance_result.get("blocked"):
                row = _manifest_row(job, "compliance_blocked")
                manifest.append(row)
                blocked += 1
                skipped_base_ids.add(base_clip_id)
                _write_json_atomic(manifest_path, manifest)
                continue

            _build_raw_assembly(job, raw_root, cfg)
            if not compliance_prefilter:
                compliance_result = _apply_compliance(job, cfg)
                if compliance_result and compliance_result.get("blocked"):
                    row = _manifest_row(job, "compliance_blocked")
                    manifest.append(row)
                    blocked += 1
                    skipped_base_ids.add(base_clip_id)
                    _write_json_atomic(manifest_path, manifest)
                    continue

            from ffmpeg_editor import edit_clip

            edit_cfg = _variant_edit_cfg(job, cfg)
            clip_words = _variant_adjusted_words(job)
            product_events = _variant_adjusted_product_events(job)
            ok = edit_clip(
                raw_clip_path=job["raw_path"],
                output_path=job["output_path"],
                moment=job["moment"],
                clip_words=clip_words,
                product_events=product_events,
                cfg=edit_cfg,
            )
            row = _manifest_row(job, "ok" if ok else "failed")
            manifest.append(row)
            if ok:
                created += 1
                created_base_ids.add(base_clip_id)
                created_base_by_product[product].add(base_clip_id)
                created_by_product[product] += 1
            else:
                failed += 1
        except Exception as exc:
            log.warning("Modular assembly failed for %s: %s", job.get("clip_id"), exc)
            row = _manifest_row(job, "failed")
            row["error"] = str(exc)
            manifest.append(row)
            failed += 1

        _write_json_atomic(manifest_path, manifest)
        if len(manifest) == 1 or len(manifest) % 10 == 0 or index == len(render_jobs):
            log.info(
                "Modular render progress: rows=%s/%s base_created=%s/%s ok=%s blocked=%s failed=%s current=%s status=%s",
                len(manifest),
                len(render_jobs),
                len(created_base_ids),
                render_limit,
                created,
                blocked,
                failed,
                job.get("clip_id"),
                manifest[-1].get("status") if manifest else "",
            )
        if progress_callback:
            progress_callback(
                "modular",
                50 + int((index / max(1, len(render_jobs))) * 40),
                f"Rendered modular {pool_examined}/{len(jobs)}",
                event="modular_clip_complete",
                clip_id=job.get("clip_id"),
                clip_status=manifest[-1].get("status"),
            )

    scores = _score_modular_outputs(render_jobs, manifest, modular_dir, cfg)
    if scores:
        _write_json_atomic(manifest_path, manifest)

    return {
        "jobs": len(jobs),
        "render_jobs": len(render_jobs),
        "created": created,
        "skipped": skipped,
        "failed": failed,
        "blocked": blocked,
        "pool_examined": pool_examined,
        "products_created": len([product for product, bases in created_base_by_product.items() if bases]),
        "manifest_path": str(manifest_path.resolve()),
        "manifest": manifest,
        "scores": scores,
    }


def _expand_modular_jobs_with_variants(jobs: list[dict[str, Any]], cfg) -> list[dict[str, Any]]:
    n_variants = _cfg_nonnegative_int(cfg, "VARIANTS_PER_CLIP", 1)
    if n_variants <= 1:
        return jobs
    try:
        from variation_engine import expand_moments_with_variants
    except Exception as exc:
        log.warning("Variation engine unavailable for modular assembly; rendering base jobs only: %s", exc)
        return jobs

    expanded_jobs: list[dict[str, Any]] = []
    variant_seed = int(getattr(cfg, "VARIANT_SEED", 42) or 42)
    for job in jobs:
        base_clip_id = str(job.get("clip_id") or "")
        base_moment = copy.deepcopy(job.get("moment") or {})
        base_moment["clip_id"] = base_clip_id
        variants = expand_moments_with_variants(
            [base_moment],
            cfg,
            n_variants=n_variants,
            seed=variant_seed,
        )
        for variant_moment in variants:
            variant = variant_moment.get("_variant")
            variant_index = getattr(variant, "variant_index", None)
            try:
                version_dir = f"v{int(variant_index)}"
            except (TypeError, ValueError):
                version_dir = _assembly_output_subdir(cfg)
            clip_id = str(variant_moment.get("clip_id") or base_clip_id)
            variant_job = copy.deepcopy(job)
            variant_job["base_clip_id"] = base_clip_id
            variant_job["clip_id"] = clip_id
            variant_job["moment"] = variant_moment
            variant_job["variant_id"] = str(getattr(variant, "variant_id", "") or "")
            variant_job["variant_index"] = variant_index
            variant_job["version_dir"] = version_dir
            variant_job["output_filename"] = f"{clip_id}_score{int(float(variant_job.get('score') or 0))}.mp4"
            variant_job["output_relative_path"] = f"{version_dir}/{variant_job['output_filename']}"
            expanded_jobs.append(variant_job)
    log.info(
        "Expanded %s modular candidate(s) x %s variant(s) = %s render job(s)",
        len(jobs),
        n_variants,
        len(expanded_jobs),
    )
    return expanded_jobs


def _job_modular_output_relative_path(job: dict[str, Any]) -> Path:
    explicit = str(job.get("output_relative_path") or "").strip()
    if explicit:
        return Path(explicit)
    return Path(str(job["output_filename"]))


def _variant_edit_cfg(job: dict[str, Any], cfg):
    variant = (job.get("moment") or {}).get("_variant")
    if variant is None:
        return cfg
    try:
        from variation_engine import apply_variant_to_cfg
    except Exception as exc:
        log.warning("Could not apply modular variant styling for %s: %s", job.get("clip_id"), exc)
        return cfg
    edit_cfg = apply_variant_to_cfg(cfg, variant)
    setattr(edit_cfg, "_variant_transforms_baked", False)
    return edit_cfg


def _variant_adjusted_words(job: dict[str, Any]) -> list[dict[str, Any]]:
    words = copy.deepcopy(job.get("clip_words") or [])
    variant = (job.get("moment") or {}).get("_variant")
    speed_ramp = float(getattr(variant, "speed_ramp", 1.0) or 1.0) if variant is not None else 1.0
    if abs(speed_ramp - 1.0) <= 0.02:
        return words
    remapped = []
    for word in words:
        mapped = dict(word)
        mapped["start"] = round(float(word.get("start", 0.0)) / speed_ramp, 6)
        mapped["end"] = round(float(word.get("end", 0.0)) / speed_ramp, 6)
        remapped.append(mapped)
    return remapped


def _variant_adjusted_product_events(job: dict[str, Any]) -> list[dict[str, Any]]:
    events = copy.deepcopy(job.get("assembled_product_events") or [])
    variant = (job.get("moment") or {}).get("_variant")
    if variant is None:
        return events
    mirror = bool(getattr(variant, "mirror", False))
    crop_x_offset = float(getattr(variant, "crop_x_offset", 0.0) or 0.0)
    speed_ramp = float(getattr(variant, "speed_ramp", 1.0) or 1.0)
    if mirror or abs(crop_x_offset) > 0.005:
        events = _remap_events_for_spatial_variant(events, mirror, crop_x_offset)
    if abs(speed_ramp - 1.0) > 0.02:
        events = _remap_events_for_speed_ramp(events, speed_ramp)
    return events


def _remap_events_for_spatial_variant(events: list[dict[str, Any]], mirror: bool, crop_x_offset: float) -> list[dict[str, Any]]:
    remapped = []
    for event in events:
        mapped = dict(event)
        frame_w = float(event.get("frame_w") or 0.0)
        frame_h = float(event.get("frame_h") or 0.0)

        def remap_bbox(bbox):
            return _remap_bbox_for_variant(bbox, frame_w, frame_h, mirror, crop_x_offset)

        for key in ("best_bbox", "start_bbox", "end_bbox"):
            if mapped.get(key):
                mapped[key] = remap_bbox(mapped.get(key))
        if mapped.get("relative_track"):
            mapped["relative_track"] = [
                {**sample, "bbox": remap_bbox(sample.get("bbox"))}
                for sample in mapped["relative_track"]
                if isinstance(sample, dict)
            ]
        remapped.append(mapped)
    return remapped


def _remap_bbox_for_variant(bbox, frame_w: float, frame_h: float, mirror: bool, crop_x_offset: float):
    if not bbox or frame_w <= 0.0 or frame_h <= 0.0:
        return bbox
    x1, y1, x2, y2 = [float(value) for value in bbox]
    if abs(crop_x_offset) > 0.005:
        crop_w = frame_w * (1.0 - abs(crop_x_offset))
        crop_x = frame_w * crop_x_offset if crop_x_offset > 0 else 0.0
        if crop_w > 1.0:
            scale_x = frame_w / crop_w
            x1 = (x1 - crop_x) * scale_x
            x2 = (x2 - crop_x) * scale_x
    if mirror:
        x1, x2 = frame_w - x2, frame_w - x1
    x1 = max(0.0, min(frame_w, x1))
    x2 = max(0.0, min(frame_w, x2))
    y1 = max(0.0, min(frame_h, y1))
    y2 = max(0.0, min(frame_h, y2))
    if x2 < x1:
        x1, x2 = x2, x1
    if y2 < y1:
        y1, y2 = y2, y1
    return [round(x1, 3), round(y1, 3), round(x2, 3), round(y2, 3)]


def _remap_events_for_speed_ramp(events: list[dict[str, Any]], speed_ramp: float) -> list[dict[str, Any]]:
    remapped = []
    for event in events:
        mapped = dict(event)
        for key in ("relative_start", "relative_end", "start_time", "end_time"):
            if mapped.get(key) is not None:
                mapped[key] = round(float(mapped[key]) / speed_ramp, 6)
        if mapped.get("relative_start") is not None and mapped.get("relative_end") is not None:
            mapped["duration"] = round(float(mapped["relative_end"]) - float(mapped["relative_start"]), 6)
        if mapped.get("relative_track"):
            mapped["relative_track"] = [
                {
                    **sample,
                    "relative_time": round(float(sample["relative_time"]) / speed_ramp, 6),
                }
                for sample in mapped["relative_track"]
                if isinstance(sample, dict) and sample.get("relative_time") is not None
            ]
        remapped.append(mapped)
    return remapped


def build_and_render_from_library(output_dir: str | Path, working_dir: str | Path, cfg, progress_callback=None) -> dict[str, Any]:
    if bool(getattr(cfg, "MODULE_REBUILD_INDEX_BEFORE_ASSEMBLY", True)):
        library = Path(getattr(cfg, "MODULE_LIBRARY_DIR", r"D:\proya_modules"))
        with library_index_lock(library, cfg):
            index = rebuild_library_index(library, cfg, write=True)
    else:
        index = read_library_index(cfg)
    jobs = build_modular_assembly_jobs(index, output_dir, cfg)
    return render_modular_assemblies(jobs, cfg, output_dir=output_dir, working_dir=working_dir, progress_callback=progress_callback)


@contextmanager
def modular_output_lock(modular_dir: Path, cfg):
    timeout = float(getattr(cfg, "MODULE_OUTPUT_LOCK_TIMEOUT", getattr(cfg, "MODULE_INDEX_LOCK_TIMEOUT", 30.0)) or 30.0)
    lock_path = modular_dir / "manifest.json.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        import portalocker

        start = time.monotonic()
        while True:
            try:
                portalocker.lock(handle, portalocker.LOCK_EX | portalocker.LOCK_NB)
                break
            except portalocker.exceptions.LockException as exc:
                if time.monotonic() - start >= timeout:
                    raise RuntimeError(f"Could not acquire modular output lock within {timeout:.0f}s: {lock_path}") from exc
                time.sleep(0.1)
        try:
            yield
        finally:
            portalocker.unlock(handle)
    finally:
        handle.close()


def _candidate_pool(candidates: list[dict[str, Any]], cfg) -> list[dict[str, Any]]:
    pool_limit = _cfg_nonnegative_int(cfg, "MODULE_ASSEMBLY_CANDIDATE_POOL", 30)
    if not pool_limit:
        return candidates
    by_product: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for job in candidates:
        by_product[str(job.get("product") or "")].append(job)
    product_order = sorted(
        by_product,
        key=lambda product: by_product[product][0].get("rank_score", 0.0),
        reverse=True,
    )
    selected = []
    while len(selected) < pool_limit:
        added = False
        for product in product_order:
            if by_product[product]:
                selected.append(by_product[product].pop(0))
                added = True
                if len(selected) >= pool_limit:
                    break
        if not added:
            break
    return selected


def _same_date_assembly_candidates(
    product: str,
    roles: dict[str, list[dict[str, Any]]],
    output_dir: str | Path,
    cfg,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    source_dates = sorted(
        {
            str(module.get("source_date") or "")
            for role_modules in roles.values()
            for module in role_modules
            if module.get("source_date")
        }
    )
    for source_date in source_dates:
        dated_roles = {
            role: [
                module
                for module in roles.get(role, [])
                if str(module.get("source_date") or "") == source_date
            ]
            for role in ROLE_FOLDERS
        }
        if not _same_date_roles_ready(dated_roles, cfg):
            _log_no_same_date_combination(product, source_date)
            continue
        before = len(candidates)
        for main in dated_roles["main"]:
            candidates.extend(_assembly_candidates_for_main(product, main, dated_roles, output_dir, cfg))
        if len(candidates) == before:
            _log_no_same_date_combination(product, source_date)
    return candidates


def _same_date_roles_ready(roles: dict[str, list[dict[str, Any]]], cfg) -> bool:
    thresholds = _assembly_role_thresholds(cfg)
    if all(limit <= 0 for limit in thresholds.values()):
        return len(roles.get("main") or []) >= 1
    return all(len(roles.get(role) or []) >= 1 for role, limit in thresholds.items() if limit > 0)


def _assembly_role_thresholds(cfg) -> dict[str, int]:
    return {
        "hook": max(0, int(getattr(cfg, "MODULAR_ASSEMBLY_READY_MIN_HOOK", 5) or 0)),
        "main": max(0, int(getattr(cfg, "MODULAR_ASSEMBLY_READY_MIN_MAIN", 3) or 0)),
        "cta": max(0, int(getattr(cfg, "MODULAR_ASSEMBLY_READY_MIN_CTA", 3) or 0)),
    }


def _assembly_candidates_for_main(
    product: str,
    main: dict[str, Any],
    roles: dict[str, list[dict[str, Any]]],
    output_dir: str | Path,
    cfg,
) -> list[dict[str, Any]]:
    hooks = roles.get("hook") or []
    ctas = roles.get("cta") or []
    candidates = []

    if hooks and ctas:
        for hook in hooks[:8]:
            for cta in ctas[:8]:
                components = [_component_from_module(hook), _component_from_module(main), _component_from_module(cta)]
                job = _build_job(product, components, output_dir, fallback_used=False, cfg=cfg)
                if job and _assembly_duration_ok(job):
                    candidates.append(job)
        return candidates

    fallback_components = _fallback_components(product, main, hooks, ctas)
    if fallback_components:
        job = _build_job(product, fallback_components, output_dir, fallback_used=True, cfg=cfg)
        if job and _assembly_duration_ok(job):
            candidates.append(job)
    return candidates


def _fallback_components(
    product: str,
    main: dict[str, Any],
    hooks: list[dict[str, Any]],
    ctas: list[dict[str, Any]],
) -> list[dict[str, Any]] | None:
    duration = float(main.get("duration") or 0.0)
    words = main.get("words") or []
    if duration <= 0 or not words:
        return None

    hook_component = _component_from_module(hooks[0]) if hooks else _fallback_slice(main, "hook", 0.0, 7.0, from_end=False)
    cta_component = _component_from_module(ctas[0]) if ctas else _fallback_slice(main, "cta", 5.0, 10.0, from_end=True)
    if hook_component is None or cta_component is None:
        return None

    main_start = 0.0
    main_end = duration
    if hook_component.get("fallback"):
        main_start = max(main_start, float(hook_component["slice_end"]))
    if cta_component.get("fallback"):
        main_end = min(main_end, float(cta_component["slice_start"]))
    if main_end - main_start < 5.0:
        return None

    main_body = _component_from_module(main, role="main", slice_start=main_start, slice_end=main_end, fallback=False)
    components = [hook_component, main_body, cta_component]
    return components


def _fallback_slice(module: dict[str, Any], role: str, min_duration: float, max_duration: float, from_end: bool) -> dict[str, Any] | None:
    words = module.get("words") or []
    duration = float(module.get("duration") or 0.0)
    if not words or duration <= 0:
        return None

    if from_end:
        slice_end = duration
        sentence_starts = [0.0]
        sentence_starts.extend(
            float(words[index + 1].get("start", 0.0))
            for index, word in enumerate(words[:-1])
            if SENTENCE_END_RE.search(str(word.get("word", "")).strip())
        )
        eligible_starts = [
            start
            for start in sentence_starts
            if min_duration <= slice_end - start <= max_duration
        ]
        if not eligible_starts:
            return None
        slice_start = min(eligible_starts, key=lambda start: abs((slice_end - start) - 7.0))
    else:
        slice_start = 0.0
        eligible_ends = [
            float(word.get("end", 0.0))
            for word in words
            if SENTENCE_END_RE.search(str(word.get("word", "")).strip())
            and min_duration <= float(word.get("end", 0.0)) - slice_start <= max_duration
        ]
        if not eligible_ends:
            return None
        slice_end = min(eligible_ends, key=lambda end: abs((end - slice_start) - 6.0))

    return _component_from_module(module, role=role, slice_start=slice_start, slice_end=slice_end, fallback=True)


def _component_from_module(
    module: dict[str, Any],
    role: str | None = None,
    slice_start: float | None = None,
    slice_end: float | None = None,
    fallback: bool = False,
) -> dict[str, Any]:
    duration = float(module.get("duration") or 0.0)
    start = 0.0 if slice_start is None else max(0.0, float(slice_start))
    end = duration if slice_end is None else min(duration, float(slice_end))
    source_start = float(module.get("start") or 0.0) + start
    source_end = float(module.get("start") or 0.0) + end
    return {
        "role": role or module.get("role"),
        "module_id": module.get("module_id"),
        "product": module.get("product"),
        "file_path": module.get("file_path"),
        "sidecar_path": module.get("sidecar_path"),
        "source_video": module.get("source_video"),
        "source_video_identity": module.get("source_video_identity"),
        "source_date": module.get("source_date"),
        "source_moment_id": module.get("source_moment_id"),
        "quality_status": module.get("quality_status"),
        "quality_score": module.get("quality_score"),
        "review_status": module.get("review_status"),
        "boundary_mode": module.get("boundary_mode"),
        "slice_start": round(start, 6),
        "slice_end": round(end, 6),
        "source_start": round(source_start, 6),
        "source_end": round(source_end, 6),
        "module_duration": round(duration, 6),
        "duration": round(max(0.0, end - start), 6),
        "confidence": float(module.get("confidence") or 0.0),
        "transcript_text": module.get("transcript_text", ""),
        "suggested_hook": module.get("suggested_hook", ""),
        "words": _slice_words(module.get("words") or [], start, end),
        "fallback": bool(fallback),
        "visual_validation_status": module.get("visual_validation_status", "not_run"),
        "visual_product_hits": int(module.get("visual_product_hits") or 0),
        "visual_product_confidence_max": float(module.get("visual_product_confidence_max") or 0.0),
        "visual_validation_reason": module.get("visual_validation_reason", ""),
        "visual_product_events": module.get("visual_product_events") or [],
    }


def _build_job(product: str, components: list[dict[str, Any]], output_dir: str | Path, fallback_used: bool, cfg=None) -> dict[str, Any] | None:
    if not components or any(float(component.get("duration", 0.0)) <= 0 for component in components):
        return None

    total_duration = sum(float(component["duration"]) for component in components)
    clip_words = _assemble_words(components)
    transcript_text = " ".join(word.get("word", "") for word in clip_words).strip()
    if not transcript_text:
        return None

    module_ids = [str(component.get("module_id") or "") for component in components]
    source_dates = sorted({str(component.get("source_date") or "") for component in components if component.get("source_date")})
    source_date = source_dates[0] if len(source_dates) == 1 else ""
    digest_payload = {
        "schema_version": ASSEMBLY_SCHEMA_VERSION,
        "product": product,
        "fallback_used": bool(fallback_used),
        "components": [
            {
                "role": component.get("role"),
                "module_id": component.get("module_id"),
                "slice_start": component.get("slice_start"),
                "slice_end": component.get("slice_end"),
                "source_start": component.get("source_start"),
                "source_end": component.get("source_end"),
                "fallback": bool(component.get("fallback")),
            }
            for component in components
        ],
    }
    digest = hashlib.sha1(json.dumps(digest_payload, sort_keys=True).encode("utf-8")).hexdigest()[:10]
    clip_id = f"mod_{product}_{digest}"
    hook_text = _hook_text_for_components(product, components, transcript_text, cfg)
    visual_product_event_count_available = _visual_event_count_for_components(product, components)
    zoom_ready = visual_product_event_count_available >= _zoom_ready_min_events(cfg)
    if bool(getattr(cfg, "MODULE_ASSEMBLY_REQUIRE_ZOOM_READY", False)) and not zoom_ready:
        return None

    rank_score = _rank_components(
        components,
        total_duration,
        cfg=cfg,
        product=product,
        visual_product_event_count_available=visual_product_event_count_available,
    )
    output_filename = f"{clip_id}_score{int(rank_score)}.mp4"
    moment = {
        "clip_id": clip_id,
        "start": 0.0,
        "end": round(total_duration, 6),
        "score": round(rank_score, 2),
        "hook": hook_text,
        "hook_overlay": {"headline": hook_text, "subtext": "", "cta": ""},
        "product": product,
        "source_date": source_date,
        "clip_type": "modular",
        "reason": "Gabungan modul hook, main, dan CTA dari library.",
        "selected_text": transcript_text,
        "keyword_category": "attention_benefits",
        "keywords_found": [],
    }
    return {
        "clip_id": clip_id,
        "output_dir": str(Path(output_dir)),
        "output_filename": output_filename,
        "output_file": _assembly_relative_output_file(output_filename, cfg),
        "version_dir": _assembly_output_subdir(cfg),
        "start": 0.0,
        "end": round(total_duration, 6),
        "duration": round(total_duration, 6),
        "score": round(rank_score, 2),
        "product": product,
        "source_date": source_date,
        "clip_type": "modular",
        "moment": moment,
        "clip_words": clip_words,
        "components": components,
        "source_module_ids": module_ids,
        "fallback_used": fallback_used,
        "rank_score": rank_score,
        "zoom_ready": zoom_ready,
        "visual_product_event_count_available": visual_product_event_count_available,
        "visual_product_event_count": 0,
        "visual_validation_statuses": _visual_statuses_for_components(components),
        "transcript_text": transcript_text,
    }


def _assembly_duration_ok(job: dict[str, Any]) -> bool:
    duration = float(job.get("duration") or 0.0)
    return 30.0 <= duration <= 50.0


def _build_raw_assembly(job: dict[str, Any], raw_root: Path, cfg) -> None:
    component_files = _materialize_component_files(job, raw_root, cfg)
    profiles = [probe_media(path) for path in component_files]
    if any(profile is None for profile in profiles):
        raise RuntimeError("Could not probe modular component files")

    raw_path = Path(job["raw_path"])
    if _profiles_match_for_stream_copy(profiles):
        _concat_stream_copy(component_files, raw_path, raw_root)
        job["concat_mode"] = "stream_copy"
        return

    normalized = []
    for index, path in enumerate(component_files):
        target = raw_root / f"{job['clip_id']}_norm_{index}.mp4"
        _normalize_component(path, target, cfg)
        normalized.append(target)
    _concat_stream_copy(normalized, raw_path, raw_root)
    job["concat_mode"] = "normalized"


def _materialize_component_files(job: dict[str, Any], raw_root: Path, cfg) -> list[Path]:
    files = []
    for index, component in enumerate(job["components"]):
        source = Path(str(component.get("file_path") or ""))
        if not source.exists():
            raise RuntimeError(f"Module file missing: {source}")
        full_slice = abs(float(component.get("slice_start", 0.0))) < 1e-6 and abs(float(component.get("slice_end", component.get("duration", 0.0))) - _module_duration(component, source)) < 0.05
        if full_slice:
            files.append(source)
            continue
        target = raw_root / f"{job['clip_id']}_slice_{index}.mp4"
        _cut_component_slice(source, target, float(component["slice_start"]), float(component["slice_end"]))
        files.append(target)
    return files


def _cut_component_slice(source: Path, target: Path, start: float, end: float) -> None:
    duration = max(0.0, end - start)
    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        f"{start:.3f}",
        "-i",
        str(source),
        "-t",
        f"{duration:.3f}",
        "-c:v",
        "libx264",
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
        str(target),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=max(30, int(duration * 6) + 30), check=False)
    if result.returncode != 0:
        raise RuntimeError(f"Component slice cut failed: {(result.stderr or '').strip()[-500:]}")


def _profiles_match_for_stream_copy(profiles: list[dict[str, Any] | None]) -> bool:
    clean = [profile for profile in profiles if profile]
    if len(clean) != len(profiles) or not clean:
        return False
    first = _concat_profile(clean[0])
    return all(_concat_profile(profile) == first for profile in clean[1:])


def _concat_profile(profile: dict[str, Any]) -> tuple[Any, ...]:
    return (
        profile.get("video_codec"),
        profile.get("audio_codec"),
        profile.get("width"),
        profile.get("height"),
        profile.get("audio_sample_rate"),
    )


def _normalize_component(source: Path, target: Path, cfg) -> None:
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(source),
        "-vf",
        "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30,setsar=1",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "23",
        "-c:a",
        "aac",
        "-ar",
        "44100",
        "-ac",
        "2",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(target),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=240, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"Component normalization failed: {(result.stderr or '').strip()[-500:]}")


def _concat_stream_copy(paths: list[Path], target: Path, raw_root: Path) -> None:
    list_path = raw_root / f"{target.stem}_concat.txt"
    list_path.write_text("".join(f"file '{_ffmpeg_concat_path(path)}'\n" for path in paths), encoding="utf-8")
    cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_path), "-c", "copy", str(target)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=240, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"Concat failed: {(result.stderr or '').strip()[-500:]}")


def _apply_compliance(job: dict[str, Any], cfg) -> dict[str, Any] | None:
    if not bool(getattr(cfg, "COMPLIANCE_ENABLED", True)):
        return None
    try:
        from compliance_checker import (
            apply_compliance_to_hook_payload,
            apply_compliance_to_words,
            check_compliance,
            compliance_path_for_clip,
            should_block_result,
            write_compliance_result,
        )
    except Exception as exc:
        log.warning("Compliance checker unavailable for modular clip: %s", exc)
        result = _compliance_unavailable_result(exc)
        job["compliance_result"] = result
        return result

    try:
        hook_payload = job["moment"].get("hook_overlay", {})
        result = check_compliance(job.get("clip_words") or [], job.get("product", "general"), hook_text=hook_payload, cfg=cfg)
        result["blocked"] = should_block_result(result, cfg)
        compliance_path = compliance_path_for_clip(job["output_path"], job["clip_id"])
        write_compliance_result(compliance_path, result)
        job["compliance_result"] = result
        job["compliance_json_path"] = str(compliance_path)
    except Exception as exc:
        log.warning("Compliance check failed closed for modular clip %s: %s", job.get("clip_id"), exc)
        result = _compliance_unavailable_result(exc)
        job["compliance_result"] = result
        return result

    if not result.get("blocked"):
        job["clip_words"] = apply_compliance_to_words(job["clip_words"], result)
        patched_hook = apply_compliance_to_hook_payload(hook_payload, result)
        job["moment"]["hook_overlay"] = patched_hook
        job["moment"]["hook"] = patched_hook.get("headline", job["moment"].get("hook", ""))
    return result


def _score_modular_outputs(jobs: list[dict[str, Any]], manifest: list[dict[str, Any]], modular_dir: Path, cfg) -> list[dict[str, Any]]:
    if not bool(getattr(cfg, "SCORER_ENABLED", True)):
        return []
    try:
        from clip_scorer import score_clip_variants, write_score_artifacts
    except Exception as exc:
        log.warning("Clip scorer unavailable for modular clips: %s", exc)
        return []

    rows_by_clip = {row.get("clip_id"): row for row in manifest if isinstance(row, dict)}
    entries = []
    for job in jobs:
        row = rows_by_clip.get(job.get("clip_id"))
        output_path = Path(str(job.get("output_path") or ""))
        if not row or row.get("status") in {"failed", "compliance_blocked"} or not output_path.exists():
            continue
        entries.append(
            {
                "clip_path": output_path,
                "transcript": job.get("clip_words") or job.get("transcript_text", ""),
                "product": job.get("product", "general"),
                "clip_id": job.get("clip_id"),
                "base_clip_id": job.get("base_clip_id") or job.get("clip_id"),
                "variant_id": job.get("variant_id") or "modular",
                "output_file": row.get("output_file"),
                "version_dir": row.get("version_dir", "modular"),
                "hook": row.get("hook", ""),
                "clip_type": "modular",
                "source_moment_score": row.get("score"),
                "compliance_passed": row.get("compliance_passed"),
                "violation_count": row.get("violation_count"),
                "auto_fixed": row.get("auto_fixed"),
                "compliance_blocked": row.get("compliance_blocked"),
                "compliance_summary": row.get("compliance_summary", ""),
                "compliance_file": row.get("compliance_file", ""),
            }
        )

    if not entries:
        return []
    scores, groups, stats = score_clip_variants(entries, cfg=cfg)
    scores_by_clip = {score.get("clip_id"): score for score in scores if isinstance(score, dict)}
    for row in manifest:
        score = scores_by_clip.get(row.get("clip_id"))
        if score:
            _attach_score(row, score, cfg)
    artifacts = write_score_artifacts(
        scores,
        modular_dir,
        groups=groups,
        optimization_stats=stats,
        cfg=cfg,
        finalize=not bool(getattr(cfg, "SCORER_VISION_ENABLED", False)),
    )
    _apply_tier_moves_to_manifest(manifest, artifacts.get("tier_move", {}))
    return scores


def _prepare_job_product_events(job: dict[str, Any], cfg) -> list[dict[str, Any]]:
    enabled = bool(getattr(cfg, "MODULE_PRODUCT_ZOOM_ENABLED", False))
    available_events = _assembly_visual_product_events(job, cfg)
    events = available_events if enabled else []
    min_events = _zoom_ready_min_events(cfg)
    job["module_product_zoom_enabled"] = enabled
    job["assembled_product_events"] = events
    job["visual_product_event_count_available"] = len(available_events)
    job["zoom_ready"] = len(available_events) >= min_events
    job["visual_product_event_count"] = len(events)
    job["product_event_status"] = "attached" if events else ("no_validated_events" if enabled else "disabled")
    return events


def _assembly_visual_product_events(job: dict[str, Any], cfg) -> list[dict[str, Any]]:
    product = canonical_product(job.get("product")) or str(job.get("product") or "")
    if not product:
        return []

    events: list[dict[str, Any]] = []
    component_offset = 0.0
    for component in job.get("components", []) or []:
        duration = max(0.0, float(component.get("duration") or 0.0))
        if str(component.get("visual_validation_status") or "") != "passed":
            component_offset += duration
            continue
        slice_start = float(component.get("slice_start") or 0.0)
        slice_end = float(component.get("slice_end") or (slice_start + duration))
        for event in component.get("visual_product_events") or []:
            if not isinstance(event, dict) or not _event_matches_product(event, product):
                continue
            remapped = _remap_visual_event(event, component_offset, slice_start, slice_end)
            if remapped:
                events.append(remapped)
        component_offset += duration
    events.sort(key=lambda event: float(event.get("relative_start") or event.get("start_time") or 0.0))
    return events


def _event_matches_product(event: dict[str, Any], product: str) -> bool:
    event_product = canonical_product(event.get("product"))
    class_product = canonical_product(event.get("class_name"))
    return product in {event_product, class_product}


def _remap_visual_event(
    event: dict[str, Any],
    component_offset: float,
    slice_start: float,
    slice_end: float,
) -> dict[str, Any] | None:
    event_start = _safe_float(event.get("relative_start", event.get("start_time")), None)
    event_end = _safe_float(event.get("relative_end", event.get("end_time", event_start)), None)
    if event_start is None or event_end is None:
        return None
    if event_end < event_start:
        event_start, event_end = event_end, event_start
    overlap_start = max(float(event_start), float(slice_start))
    overlap_end = min(float(event_end), float(slice_end))
    if overlap_end < overlap_start:
        return None

    assembled_start = round(component_offset + (overlap_start - slice_start), 3)
    assembled_end = round(component_offset + (overlap_end - slice_start), 3)
    relative_track = _remap_visual_track(event, component_offset, slice_start, slice_end)
    remapped = {
        key: value
        for key, value in event.items()
        if key not in {"relative_start", "relative_end", "start_time", "end_time", "relative_track", "track"}
    }
    remapped.update(
        {
            "start_time": assembled_start,
            "end_time": assembled_end,
            "relative_start": assembled_start,
            "relative_end": assembled_end,
            "duration": round(max(0.0, assembled_end - assembled_start), 3),
            "relative_track": relative_track,
        }
    )
    if not relative_track:
        remapped["relative_track"] = []
    return remapped


def _remap_visual_track(
    event: dict[str, Any],
    component_offset: float,
    slice_start: float,
    slice_end: float,
) -> list[dict[str, Any]]:
    raw_track = event.get("relative_track") or event.get("track") or []
    remapped = []
    for sample in raw_track:
        if not isinstance(sample, dict):
            continue
        sample_t = _safe_float(sample.get("relative_time", sample.get("time")), None)
        if sample_t is None or sample_t < slice_start or sample_t > slice_end:
            continue
        remapped.append(
            {
                "relative_time": round(component_offset + (sample_t - slice_start), 3),
                "bbox": sample.get("bbox"),
                "confidence": _safe_float(sample.get("confidence"), 0.0) or 0.0,
            }
        )
    return remapped


def _manifest_row(job: dict[str, Any], status: str) -> dict[str, Any]:
    product_events = job.get("assembled_product_events") or []
    visual_statuses = job.get("visual_validation_statuses") or _visual_statuses_for_components(job.get("components", []))
    row = {
        "schema_version": ASSEMBLY_SCHEMA_VERSION,
        "clip_id": job.get("clip_id"),
        "base_clip_id": job.get("base_clip_id") or job.get("clip_id"),
        "variant_id": job.get("variant_id") or "",
        "variant_index": job.get("variant_index"),
        "version_dir": job.get("version_dir", "modular"),
        "output_file": job.get("output_file"),
        "start": 0.0,
        "end": job.get("duration"),
        "duration": job.get("duration"),
        "score": job.get("score"),
        "hook": job.get("moment", {}).get("hook", ""),
        "hook_overlay": job.get("moment", {}).get("hook_overlay", {}),
        "product": job.get("product"),
        "source_date": job.get("source_date", ""),
        "clip_type": "modular",
        "reason": job.get("moment", {}).get("reason", ""),
        "product_events": len(product_events),
        "module_product_zoom_enabled": bool(job.get("module_product_zoom_enabled", False)),
        "zoom_ready": bool(job.get("zoom_ready", False)),
        "visual_product_event_count_available": int(job.get("visual_product_event_count_available") or 0),
        "visual_product_event_count": len(product_events),
        "visual_product_events": product_events,
        "visual_product_event_status": job.get("product_event_status", ""),
        "visual_validation_statuses": visual_statuses,
        "visual_product_confidence_max": max(
            [float(component.get("visual_product_confidence_max") or 0.0) for component in job.get("components", [])] or [0.0]
        ),
        "status": status,
        "candidate_rank": job.get("candidate_rank"),
        "source_module_ids": job.get("source_module_ids", []),
        "components": [
            {
                "role": component.get("role"),
                "module_id": component.get("module_id"),
                "source_date": component.get("source_date", ""),
                "duration": component.get("duration"),
                "fallback": bool(component.get("fallback")),
                "visual_validation_status": component.get("visual_validation_status", "not_run"),
                "visual_product_hits": int(component.get("visual_product_hits") or 0),
            }
            for component in job.get("components", [])
        ],
        "fallback_used": bool(job.get("fallback_used")),
        "concat_mode": job.get("concat_mode", ""),
    }
    variant = (job.get("moment") or {}).get("_variant")
    broll_intro_path = str(getattr(variant, "broll_intro_path", "") or "") if variant is not None else ""
    if broll_intro_path:
        row["broll_intro"] = True
        row["broll_intro_file"] = broll_intro_path
        row["broll_intro_duration"] = float(getattr(variant, "broll_intro_duration", 0.0) or 0.0)
        row["broll_intro_product"] = str(getattr(variant, "broll_intro_product", "") or "")
    result = job.get("compliance_result")
    if isinstance(result, dict):
        row["compliance_passed"] = bool(result.get("passed", False))
        row["violation_count"] = int(result.get("violation_count") or 0)
        row["auto_fixed"] = bool(result.get("auto_fixed", False))
        row["compliance_blocked"] = bool(result.get("blocked", False))
        row["compliance_summary"] = str(result.get("compliance_summary") or "")
        row["blocked_reason"] = _blocked_reason(result) if result.get("blocked") else ""
        if job.get("compliance_json_path"):
            row["compliance_file"] = job["compliance_json_path"]
    return row


def _load_index_modules_with_words(index: dict[str, Any], cfg) -> list[dict[str, Any]]:
    modules = []
    for summary in index.get("modules", []) or []:
        if not isinstance(summary, dict):
            continue
        sidecar = Path(str(summary.get("sidecar_path") or module_sidecar_path(Path(str(summary.get("file_path") or "")))))
        try:
            record = json.loads(sidecar.read_text(encoding="utf-8"))
        except Exception:
            record = dict(summary)
            record.setdefault("words", [])
        record.update(module_quality_fields(record, cfg))
        if Path(str(record.get("file_path") or "")).exists():
            modules.append(record)
    return modules


def _module_allowed_for_assembly(module: dict[str, Any], cfg) -> bool:
    if not _module_has_usable_words(module):
        return False
    if bool(getattr(cfg, "MODULE_ASSEMBLY_REQUIRE_APPROVED", True)):
        return _quality_allows_assembly(module.get("quality_status"))
    if (
        str(module.get("boundary_mode") or "") == "word_boundary_fallback"
        and bool(getattr(cfg, "MODULE_WORD_FALLBACK_REVIEW_REQUIRED", True))
        and module.get("review_status") != QUALITY_APPROVED
    ):
        return False
    return True


def _module_has_usable_words(module: dict[str, Any]) -> bool:
    return any(
        isinstance(word, dict)
        and str(word.get("word", "")).strip()
        and _safe_float(word.get("end"), 0.0) is not None
        and _safe_float(word.get("end"), 0.0) >= _safe_float(word.get("start"), 0.0)
        for word in (module.get("words") or [])
    )


def _product_ready_for_assembly(roles: dict[str, list[dict[str, Any]]], cfg) -> bool:
    thresholds = _assembly_role_thresholds(cfg)
    min_sources = max(0, int(getattr(cfg, "MODULE_ASSEMBLY_MIN_SOURCE_VIDEOS", 2) or 0))
    if any(len(roles.get(role) or []) < thresholds.get(role, 0) for role in ROLE_FOLDERS):
        return False
    sources = {
        _module_source_key(module)
        for role in ROLE_FOLDERS
        for module in roles.get(role, [])
    }
    sources.discard("")
    return len(sources) >= min_sources


def _module_rank(module: dict[str, Any]) -> tuple[float, ...]:
    quality_bonus = 1.0 if _quality_allows_assembly(module.get("quality_status")) else 0.0
    boundary_bonus = 0.25 if module.get("boundary_mode") == "sentence" else 0.0
    return (
        quality_bonus,
        float(module.get("quality_score") or 0.0),
        boundary_bonus,
        float(module.get("confidence") or 0.0),
        float(module.get("duration") or 0.0),
    )


def _quality_allows_assembly(status: Any) -> bool:
    return str(status or "").strip() in {QUALITY_APPROVED, QUALITY_NO_VISUAL_EVENTS}


def _module_source_date(module: dict[str, Any], warn: bool = False) -> str:
    for key in ("source_date", "source_video_date"):
        source_date = _normalize_source_date_value(module.get(key))
        if source_date:
            return source_date
    source_video = module.get("source_video")
    source_date = source_date_from_source_video(source_video)
    if source_date:
        return source_date
    if warn:
        log.warning(
            "Module %s has no usable source date and source_video %r does not match YYYY-MM-DD-HH-MM-SS.mp4; excluding from modular assembly",
            module.get("module_id") or module.get("file_path") or "<unknown>",
            source_video,
        )
    return ""


def _normalize_source_date_value(value: Any) -> str:
    text = str(value or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    if re.fullmatch(r"\d{8}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return ""


def _assembly_output_subdir(cfg) -> str:
    value = getattr(cfg, "MODULE_ASSEMBLY_OUTPUT_SUBDIR", "modular")
    if value is None:
        return "modular"
    return str(value).strip().strip("/\\")


def _assembly_relative_output_file(output_filename: str | Path, cfg) -> str:
    output_name = str(output_filename).replace("\\", "/")
    subdir = _assembly_output_subdir(cfg)
    if not subdir:
        return output_name
    return f"{subdir}/{output_name}"


def _log_no_same_date_combination(product: str, source_date: str) -> None:
    log.warning(
        "No same-date hook+main+cta combination available for %s on %s — skipping assembly",
        product,
        source_date,
    )


def _rank_components(
    components: list[dict[str, Any]],
    total_duration: float,
    cfg=None,
    product: str | None = None,
    visual_product_event_count_available: int | None = None,
) -> float:
    confidence = sum(float(component.get("confidence") or 0.0) for component in components) / max(1, len(components))
    quality = sum(float(component.get("quality_score") or 0.0) for component in components) / max(1, len(components))
    source_keys = {_component_source_key(component) for component in components}
    source_keys.discard("")
    diversity_bonus = min(1.5, max(0, len(source_keys) - 1) * 0.75)
    duration_score = max(0.0, 2.0 - abs(total_duration - 40.0) / 10.0)
    fallback_penalty = 0.75 if any(component.get("fallback") for component in components) else 0.0
    boundary_penalty = 0.5 if any(component.get("boundary_mode") == "word_boundary_fallback" for component in components) else 0.0
    visual_adjustment = sum(_visual_rank_adjustment(component) for component in components) / max(1, len(components))
    visual_event_bonus = 0.0
    if cfg is not None and bool(getattr(cfg, "MODULE_PRODUCT_ZOOM_ENABLED", False)):
        event_count = visual_product_event_count_available
        if event_count is None:
            event_count = _visual_event_count_for_components(str(product or ""), components)
        if int(event_count or 0) >= _zoom_ready_min_events(cfg):
            visual_event_bonus = float(getattr(cfg, "MODULE_ASSEMBLY_VISUAL_EVENT_BONUS", 0.75) or 0.0)
    return round(
        min(
            10.0,
            confidence * 5.0
            + quality * 0.2
            + diversity_bonus
            + duration_score
            + visual_adjustment
            + visual_event_bonus
            - fallback_penalty
            - boundary_penalty,
        ),
        2,
    )


def _visual_rank_adjustment(component: dict[str, Any]) -> float:
    status = str(component.get("visual_validation_status") or "not_run").strip().lower()
    if status == "passed":
        return 0.35
    if status == "failed":
        return -0.75
    return 0.0


def _visual_event_count_for_components(product: str, components: list[dict[str, Any]]) -> int:
    if not product:
        return 0
    return len(_assembly_visual_product_events({"product": product, "components": components}, cfg=None))


def _zoom_ready_min_events(cfg) -> int:
    return max(1, int(getattr(cfg, "MODULE_ASSEMBLY_ZOOM_READY_MIN_EVENTS", 1) or 1))


def _visual_statuses_for_components(components: list[dict[str, Any]]) -> dict[str, str]:
    return {
        str(component.get("role") or ""): str(component.get("visual_validation_status") or "not_run")
        for component in components or []
    }


def _module_source_key(module: dict[str, Any]) -> str:
    identity = module.get("source_video_identity")
    if isinstance(identity, dict):
        key = "|".join(str(identity.get(part, "")) for part in ("path", "size", "mtime_ns"))
        if key.strip("|"):
            return key
    return str(module.get("source_video") or "")


def _component_source_key(component: dict[str, Any]) -> str:
    source = _module_source_key(component)
    moment = str(component.get("source_moment_id") or component.get("module_id") or "")
    return f"{source}|{moment}" if source or moment else ""


def _assemble_words(components: list[dict[str, Any]]) -> list[dict[str, Any]]:
    offset = 0.0
    assembled = []
    for component in components:
        for word in component.get("words") or []:
            assembled.append(
                {
                    "word": str(word.get("word", "")).strip(),
                    "start": round(offset + float(word.get("start", 0.0)), 6),
                    "end": round(offset + float(word.get("end", 0.0)), 6),
                }
            )
        offset += float(component.get("duration") or 0.0)
    return [word for word in assembled if word["word"]]


def _slice_words(words: list[dict[str, Any]], start: float, end: float) -> list[dict[str, Any]]:
    sliced = []
    for word in words or []:
        word_start = float(word.get("start", 0.0))
        word_end = float(word.get("end", word_start))
        if word_start >= start - 1e-6 and word_end <= end + 1e-6:
            sliced.append(
                {
                    "word": str(word.get("word", "")).strip(),
                    "start": round(word_start - start, 6),
                    "end": round(word_end - start, 6),
                }
            )
    return [word for word in sliced if word["word"]]


def _hook_text_for_components(product: str, components: list[dict[str, Any]], transcript_text: str, cfg) -> str:
    hook = components[0]
    suggested = str(hook.get("suggested_hook") or "").strip()
    safe_hooks_enabled = bool(getattr(cfg, "MODULE_ASSEMBLY_SAFE_HOOKS_ENABLED", True)) if cfg is not None else True
    if suggested:
        if safe_hooks_enabled and _hook_text_risky(suggested):
            return SAFE_HOOKS.get(product, "Cek Step Skincare Ini")
        return suggested[:80]
    words = transcript_text.split()
    fallback = " ".join(words[:8])[:80] or "Momen PROYA pilihan"
    if safe_hooks_enabled and _hook_text_risky(fallback):
        return SAFE_HOOKS.get(product, "Cek Step Skincare Ini")
    return fallback


def _hook_text_risky(text: str) -> bool:
    return bool(RISKY_HOOK_RE.search(str(text or "")))


def _blocked_reason(result: dict[str, Any]) -> str:
    violations = result.get("violations") if isinstance(result, dict) else []
    for violation in violations or []:
        if not isinstance(violation, dict):
            continue
        severity = str(violation.get("severity") or "")
        if severity in {"high", "medium"}:
            original = str(violation.get("original_text") or "").strip()
            violation_type = str(violation.get("violation_type") or "").strip()
            return ": ".join(part for part in (severity, violation_type, original) if part)
    return str(result.get("compliance_summary") or "compliance_blocked")


def _compliance_unavailable_result(exc: Exception) -> dict[str, Any]:
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
        "compliance_summary": f"Compliance unavailable; modular candidate blocked: {exc}",
        "source": "system_fail_closed",
        "qwen_called": False,
    }


def _module_duration(component: dict[str, Any], source: Path) -> float:
    duration = float(component.get("module_duration") or 0.0)
    if duration > 0:
        return duration
    probe = probe_media(source)
    return float((probe or {}).get("duration") or component.get("duration") or 0.0)


def _existing_modular_output_valid(job: dict[str, Any]) -> bool:
    if bool(job.get("module_product_zoom_enabled")) and job.get("assembled_product_events"):
        return False
    path = Path(str(job.get("output_path") or ""))
    probe = probe_media(path)
    if not probe or not probe.get("has_video") or not probe.get("has_audio"):
        return False
    duration = _safe_float(probe.get("duration"), None)
    expected = _safe_float(job.get("duration"), None)
    if duration is None or duration <= 0:
        return False
    if expected is not None and abs(duration - expected) > 2.0:
        return False
    return True


def _cfg_nonnegative_int(cfg, name: str, default: int) -> int:
    value = getattr(cfg, name, None)
    if value is None:
        value = default
    return max(0, int(value))


def _safe_float(value: Any, default: float | None = 0.0) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _ffmpeg_concat_path(path: Path) -> str:
    text = str(path.resolve()).replace("\\", "/")
    return text.replace("'", "'\\''")


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _attach_score(row: dict[str, Any], score: dict[str, Any], cfg) -> None:
    row["scorer_base_clip_id"] = score.get("base_clip_id")
    row["scorer_variant_id"] = score.get("variant_id")
    row["scorer_total_score"] = score.get("total_score")
    row["scorer_content_score"] = score.get("content_score")
    row["scorer_quality_score"] = score.get("quality_score")
    row["scorer_engagement_score"] = score.get("engagement_score")
    row["scorer_host_focus_score"] = score.get("host_focus_score")
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


def _apply_tier_moves_to_manifest(manifest: list[dict[str, Any]], tier_move: dict[str, Any]) -> None:
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
