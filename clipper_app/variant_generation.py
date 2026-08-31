from __future__ import annotations

import copy
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from clipper_app.modular_renderer.media import probe_media


@dataclass(frozen=True)
class BaseClipArtifact:
    """The ordinary, materialized base-clip contract consumed by Variants."""

    clip_id: str
    path: Path
    product: str
    transcript_words: tuple[dict[str, Any], ...] = ()
    hook: str = ""


class _VariantRuntimeConfig:
    def __init__(self, base: Any, profile: dict[str, Any]):
        object.__setattr__(self, "_base", base)
        object.__setattr__(self, "_overrides", {
            "COMPLIANCE_ENABLED": False,
            "RENDER_REQUIRE_TARGET_SIZE": False,
            "VARIANT_SELECTION_MODE": "custom",
            "VARIANTS_PER_CLIP": 6,
            "_variation_profile_override": copy.deepcopy(profile),
        })

    def __getattr__(self, name: str) -> Any:
        overrides = object.__getattribute__(self, "_overrides")
        return overrides[name] if name in overrides else getattr(object.__getattribute__(self, "_base"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        object.__getattribute__(self, "_overrides")[name] = value


def generate_base_clip_variants(
    artifact: BaseClipArtifact,
    output_directory: str | Path,
    cfg: Any,
    profile: dict[str, Any],
    *,
    expected_count: int = 6,
    on_variant: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    """Run the existing profile expansion and clip-render path for one base MP4.

    This function deliberately stops below compliance, scoring, export and delivery.
    It has no dependency on modular repositories or modular identifiers.
    """

    from main import _build_clip_job, _process_clip_job
    from variation_engine import expand_moments_with_variants

    base_path = artifact.path.resolve(strict=True)
    media = probe_media(base_path)
    duration = float(media.get("duration") or 0.0)
    if duration <= 0.5:
        raise ValueError("Base clip duration is invalid")

    runtime_cfg = _VariantRuntimeConfig(cfg, profile)
    moment = {
        "clip_id": artifact.clip_id,
        "start": 0.0,
        "end": duration,
        "score": 0,
        "product": artifact.product,
        "clip_type": "modular_base",
        "hook": artifact.hook or artifact.clip_id,
    }
    expanded = expand_moments_with_variants(
        [moment], runtime_cfg, n_variants=expected_count, selection_mode="custom",
        source_identity=str(base_path),
    )
    if len(expanded) != expected_count:
        raise ValueError(
            f"Selected variation profile resolves to {len(expanded)} variants; {expected_count} are required"
        )

    output_root = Path(output_directory).resolve()
    raw_root = output_root / "_work" / "raw"
    output_root.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    for index, variant_moment in enumerate(expanded):
        job = _build_clip_job(variant_moment, index, str(output_root), raw_root)
        started = time.perf_counter()
        result = _process_clip_job(
            job, str(base_path), list(artifact.transcript_words), [], False, runtime_cfg,
        )
        elapsed = time.perf_counter() - started
        output_path = Path(job["output_path"])
        if result.get("status") not in {"ok", "skipped"} or not output_path.is_file():
            details = ": ".join(filter(None, (
                str(job.get("render_failure_stage") or ""),
                str(job.get("render_error_code") or ""),
                str(job.get("render_error_message") or ""),
            )))
            raise RuntimeError(
                f"Variant {index} failed in the existing renderer" + (f": {details}" if details else "")
            )
        rendered = probe_media(output_path)
        variant = variant_moment.get("_variant")
        row = {
            "variant_index": index,
            "variant_id": str(getattr(variant, "variant_id", f"v{index}")),
            "variant_name": str(getattr(variant, "display_name", f"Variant {index + 1}")),
            "output_path": str(output_path.resolve()),
            "duration": float(rendered.get("duration") or 0.0),
            "width": int(rendered.get("width") or 0),
            "height": int(rendered.get("height") or 0),
            "has_video": bool(rendered.get("has_video", True)),
            "has_audio": bool(rendered.get("has_audio", False)),
            "file_size": output_path.stat().st_size,
            "generation_seconds": elapsed,
        }
        results.append(row)
        if on_variant:
            on_variant(dict(row))
    return results


def safe_clip_id(composition_id: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9_-]+", "-", composition_id).strip("-_")
    return f"modular_{clean[:48] or 'base'}"
