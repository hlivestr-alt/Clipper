from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import Any, Callable

from clipper_app.modular_planner import ModularPlannerService
from clipper_app.modular_renderer import ModularRendererService
from clipper_app.variant_generation import BaseClipArtifact, generate_base_clip_variants, safe_clip_id
from product_broll import canonical_product
from variation_profile import list_presets, load_active_profile, load_preset

from clipper_app.modular_variant_pilot.transcript_bridge import (
    BRIDGE_VERSION,
    SourceTranscriptResolver,
    bridge_composition_words,
)


class ModularVariantServiceError(RuntimeError):
    pass


def _file_identity(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class ModularBaseAdapter:
    """Modular-aware boundary that produces the ordinary BaseClipArtifact contract."""

    def __init__(self, cfg: Any, *, renderer: ModularRendererService, planner: ModularPlannerService):
        working = Path(str(getattr(cfg, "WORKING_DIR", "working") or "working"))
        self.renderer = renderer
        self.planner = planner
        self.transcript_resolver = SourceTranscriptResolver(working / "modular_library.sqlite3")

    def adapt(self, render_run_id: str, composition_id: str) -> dict[str, Any]:
        run = self.renderer.repository.get_run(render_run_id)
        if not self.compatible_renderer(str(run["renderer_version"])):
            raise ModularVariantServiceError("Render item is not compatible with modular-renderer-v1.1")
        internal = self.renderer.repository.item(render_run_id, composition_id)
        if internal["status"] != "completed":
            raise ModularVariantServiceError("Only completed modular render items are eligible")
        diagnostics = json.loads(str(internal.get("diagnostics_json") or "{}"))
        if not diagnostics and str(run["renderer_version"]) == "modular-renderer-v1.1":
            raise ModularVariantServiceError("Renderer validation diagnostics are missing")
        planner_run = self.planner.get_run(run["planner_run_id"])
        if planner_run["status"] != "approved":
            raise ModularVariantServiceError("Planner lineage is not approved")
        manifest = self.planner.manifest(run["planner_run_id"], public=False)
        composition = next(
            (row for row in manifest["payload"].get("compositions", []) if row["composition_id"] == composition_id),
            None,
        )
        if composition is None:
            raise ModularVariantServiceError("Composition is not in the approved manifest")
        path = self.renderer.media_path(render_run_id, composition_id)
        product = canonical_product(internal["product"])
        if product is None or product != canonical_product(manifest["payload"]["product"]):
            raise ModularVariantServiceError("Render product does not match approved composition metadata")
        words, hook, transcript_diagnostics = bridge_composition_words(
            composition,
            self.transcript_resolver,
            rendered_duration=(
                float(internal["rendered_duration"])
                if internal.get("rendered_duration") is not None
                else None
            ),
        )
        return {
            "render_run_id": render_run_id,
            "modular_render_item_id": f"{render_run_id}:{composition_id}",
            "planner_run_id": run["planner_run_id"],
            "planner_manifest_id": run["planner_manifest_id"],
            "composition_id": composition_id,
            "composition": composition,
            "product": product,
            "renderer_version": run["renderer_version"],
            "base_path": str(path),
            "base_identity": _file_identity(path),
            "ordinal": int(internal["ordinal"]),
            "rendered_duration": float(internal.get("rendered_duration") or 0.0),
            "transcript_words": words,
            "hook_text": hook,
            "transcript_diagnostics": transcript_diagnostics,
        }

    @staticmethod
    def compatible_renderer(version: str) -> bool:
        match = re.fullmatch(r"modular-renderer-v(\d+)(?:\.(\d+))?", version)
        return bool(match and (int(match.group(1)), int(match.group(2) or 0)) >= (1, 1))


class ModularVariantService:
    """Production-safe adapter around the existing modular-agnostic Variants entry point."""

    bridge_version = BRIDGE_VERSION

    def __init__(
        self,
        cfg: Any,
        *,
        renderer: ModularRendererService,
        planner: ModularPlannerService,
        generator: Callable[..., list[dict[str, Any]]] = generate_base_clip_variants,
    ):
        self.cfg = cfg
        self.adapter = ModularBaseAdapter(cfg, renderer=renderer, planner=planner)
        self.generator = generator

    def profiles(self) -> dict[str, Any]:
        active = load_active_profile(self.cfg)
        profiles = [{
            "profile_id": "active",
            "name": "Active variation profile",
            "revision": active["revision"],
            "variant_count": len(active.get("variants", [])),
        }]
        for row in list_presets(self.cfg):
            profile = load_preset(self.cfg, row["preset_id"])
            profiles.append({
                "profile_id": row["preset_id"],
                "name": row["name"],
                "revision": profile["revision"],
                "variant_count": len(profile.get("variants", [])),
            })
        return {"profiles": profiles}

    def freeze_profile(self, profile_id: str) -> dict[str, Any]:
        profile = load_active_profile(self.cfg) if profile_id == "active" else load_preset(self.cfg, profile_id)
        if not profile.get("variants"):
            raise ModularVariantServiceError("The selected Variants profile has no variants")
        return copy.deepcopy(profile)

    def generate(
        self,
        adapted: dict[str, Any],
        output_directory: str | Path,
        profile: dict[str, Any],
        *,
        production_job_id: str,
        on_variant: Callable[[dict[str, Any]], None] | None = None,
    ) -> list[dict[str, Any]]:
        expected = len(profile.get("variants", []))
        if expected < 1:
            raise ModularVariantServiceError("Frozen Variants profile is empty")
        artifact = BaseClipArtifact(
            clip_id=safe_clip_id(adapted["composition_id"]),
            path=Path(adapted["base_path"]),
            product=adapted["product"],
            transcript_words=tuple(adapted["transcript_words"]),
            hook=adapted["hook_text"],
        )

        def attach(row: dict[str, Any]) -> None:
            row["media_id"] = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"modular-production:{production_job_id}:{adapted['composition_id']}:{row['variant_index']}",
            ).hex
            if on_variant:
                on_variant(dict(row))

        rows = self.generator(
            artifact,
            output_directory,
            self.cfg,
            profile,
            expected_count=expected,
            on_variant=attach,
        )
        for row in rows:
            row.setdefault("media_id", uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"modular-production:{production_job_id}:{adapted['composition_id']}:{row['variant_index']}",
            ).hex)
        return rows
