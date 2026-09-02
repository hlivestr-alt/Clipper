from __future__ import annotations

import hashlib
import json
import queue
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from clipper_app.application.services import ComplianceService, ExportPackagingService, ScoringService
from clipper_app.contracts.modular_planner_models import ModularPlannerRunCreateRequest
from clipper_app.contracts.modular_production_models import (
    ModularProductionContinueRequest,
    ModularProductionJobCreateRequest,
)
from clipper_app.contracts.modular_renderer_models import ModularRenderRunCreateRequest
from clipper_app.contracts.models import ComplianceScanCommand, ScoringCommand
from clipper_app.modular_planner import ModularPlannerService, PlannerConflictError
from clipper_app.modular_renderer import ModularRendererService
from clipper_app.modular_scanner.constants import ANALYZER_VERSION
from clipper_app.modular_scanner.service import production_is_active
from clipper_app.modular_variants import ModularVariantService
from clipper_app.modular_variant_pilot.transcript_bridge import BRIDGE_VERSION

from .repository import ModularProductionRepository, utc_now


class ModularProductionConflict(RuntimeError):
    pass


PLANNER_VERSION = "modular-planner-v1.1"
RENDERER_VERSION = "modular-renderer-v1.1"
CANONICAL_PRODUCTS = ("cleanser", "toner", "serum", "eye_cream", "mask", "skin_cream")


def allocate_products(requested_count: int, rotation_identity: str) -> dict[str, int]:
    """Return a stable, balanced allocation whose remainder start rotates by identity."""
    if requested_count < len(CANONICAL_PRODUCTS):
        raise ValueError("All Products requires at least 6 base videos")
    minimum, remainder = divmod(requested_count, len(CANONICAL_PRODUCTS))
    rotation_hash = 2166136261
    for byte in rotation_identity.encode("utf-8"):
        rotation_hash = ((rotation_hash ^ byte) * 16777619) & 0xFFFFFFFF
    offset = rotation_hash % len(CANONICAL_PRODUCTS)
    allocation = {product: minimum for product in CANONICAL_PRODUCTS}
    for index in range(remainder):
        allocation[CANONICAL_PRODUCTS[(offset + index) % len(CANONICAL_PRODUCTS)]] += 1
    return allocation


class ModularProductionService:
    """Persistent orchestration over the validated modular and Standard downstream services."""

    def __init__(
        self,
        cfg: Any,
        *,
        planner: ModularPlannerService,
        renderer: ModularRendererService,
        variants: ModularVariantService | None = None,
        compliance: ComplianceService | None = None,
        scoring: ScoringService | None = None,
        exports: ExportPackagingService | None = None,
        repository: ModularProductionRepository | None = None,
        standard_production_active: Callable[[], bool] | None = None,
        poll_seconds: float = 1.0,
        start_worker: bool = True,
    ):
        self.cfg = cfg
        working = Path(str(getattr(cfg, "WORKING_DIR", "working") or "working")).resolve()
        output = Path(str(getattr(cfg, "OUTPUT_DIR", r"D:\output_clips") or r"D:\output_clips")).resolve()
        self.working_root = working / "modular_production"
        self.output_root = output
        self.working_root.mkdir(parents=True, exist_ok=True)
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.planner = planner
        self.renderer = renderer
        self.variants = variants or ModularVariantService(cfg, renderer=renderer, planner=planner)
        self.compliance = compliance or ComplianceService()
        self.scoring = scoring or ScoringService()
        self.exports = exports or ExportPackagingService()
        self.repository = repository or ModularProductionRepository(working / "modular_production.sqlite3")
        self.standard_production_active = standard_production_active or (lambda: production_is_active(cfg))
        self.poll_seconds = max(0.05, poll_seconds)
        self._tasks: queue.Queue[str | None] = queue.Queue()
        self._queued: set[str] = set()
        self._guard = threading.Lock()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        for job_id in self.repository.resumable_ids():
            if self.repository.get_job(job_id, internal=True)["status"] != "awaiting_review":
                self._enqueue(job_id)
        if start_worker:
            self.start_worker()

    def start_worker(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(target=self._worker_loop, name="modular-production", daemon=True)
        self._worker.start()

    def close(self) -> None:
        self._stop.set()
        self._tasks.put(None)
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=3)

    def profiles(self) -> dict[str, Any]:
        return self.variants.profiles()

    def create_job(self, request: ModularProductionJobCreateRequest) -> tuple[dict[str, Any], bool]:
        settings = request.model_dump(mode="json")
        profile = self.variants.freeze_profile(request.variant_profile_id)
        profile_revision = str(profile.get("revision") or self._json_hash(profile))
        variant_count = len(profile.get("variants", []))
        identity = {key: value for key, value in settings.items() if key != "explicit_rerun"}
        identity["variant_profile_revision"] = profile_revision
        request_key = self._json_hash(identity)
        active = self.repository.find_active(request_key)
        if active and not request.explicit_rerun:
            return self.get_job(active["job_id"]), True

        prior = self.repository.latest_matching(request_key)
        job_id = uuid.uuid4().hex
        if settings["product"] == "all_products":
            allocation = allocate_products(
                settings["requested_base_count"], str(settings.get("seed") or job_id),
            )
            product_scope = "all"
        else:
            allocation = {settings["product"]: settings["requested_base_count"]}
            product_scope = "single"
        subflows = {
            product: {
                "product": product, "requested_base_count": count, "generated_base_count": 0,
                "rendered_base_count": 0, "failed_base_count": 0, "generated_variant_count": 0,
                "failed_variant_count": 0, "planner_run_id": None, "planner_manifest_id": None,
                "render_run_id": None, "status": "planning", "warnings": [],
            }
            for product, count in allocation.items()
        }
        output_directory = self.output_root / f"modular_{job_id}"
        working_directory = self.working_root / job_id
        output_directory.mkdir(parents=True, exist_ok=True)
        working_directory.mkdir(parents=True, exist_ok=True)
        try:
            self.repository.create_job({
                "job_id": job_id,
                "request_key": request_key,
                "workflow_mode": settings["workflow_mode"],
                "product": settings["product"],
                "product_scope": product_scope,
                "product_allocation_json": allocation,
                "product_subflows_json": subflows,
                "requested_base_count": settings["requested_base_count"],
                "variants_per_base": variant_count,
                "variant_profile_id": request.variant_profile_id,
                "variant_profile_revision": profile_revision,
                "variant_profile_json": profile,
                "settings_json": settings,
                "status": "planning",
                "current_stage": "planning",
                "output_directory": str(output_directory),
                "working_directory": str(working_directory),
                "rerun_of_job_id": prior["job_id"] if request.explicit_rerun and prior else None,
            })
        except Exception:
            active = self.repository.find_active(request_key)
            if active and not request.explicit_rerun:
                return self.get_job(active["job_id"]), True
            raise
        self._enqueue(job_id)
        return self.get_job(job_id), False

    def get_job(self, job_id: str) -> dict[str, Any]:
        return self._decorate_job(self.repository.get_job(job_id))

    def list_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        return [self._decorate_job(job) for job in self.repository.list_jobs(limit)]

    def _decorate_job(self, job: dict[str, Any]) -> dict[str, Any]:
        plans = []
        for product, subflow in job.get("product_subflows", {}).items():
            run_id = subflow.get("planner_run_id")
            if not run_id:
                continue
            try:
                run = self.planner.get_run(run_id)
                plans.append({"product": product, **run})
            except (KeyError, FileNotFoundError):
                plans.append({"product": product, "planner_run_id": run_id, "status": subflow.get("status")})
        job["product_plans"] = plans
        return job

    def continue_job(
        self, job_id: str, request: ModularProductionContinueRequest,
    ) -> dict[str, Any]:
        job = self.repository.get_job(job_id, internal=True)
        if job["workflow_mode"] != "review_first" or job["status"] != "awaiting_review":
            raise ModularProductionConflict("Only a Review First job awaiting review can continue")
        revisions = request.expected_planner_revisions or {}
        subflows = dict(job["product_subflows"])
        for product, subflow in subflows.items():
            if int(subflow.get("generated_base_count") or 0) < 1:
                subflows[product] = {**subflow, "status": "planner_shortfall"}
                continue
            run = self.planner.get_run(subflow["planner_run_id"])
            if run["status"] == "draft":
                revision = revisions.get(product, request.expected_planner_revision)
                if revision is None:
                    raise ModularProductionConflict(f"Approve the {product} planner run or supply its current revision")
                run = self.planner.approve(run["planner_run_id"], revision)
            manifest = self.planner.manifest(run["planner_run_id"], public=False)
            self._record_approval(job_id, product, manifest)
            actual = len(manifest["payload"].get("compositions", []))
            subflows[product] = {
                **subflow, "planner_manifest_id": manifest["manifest_id"], "status": "approved",
                "generated_base_count": actual,
            }
        self.repository.update_job(job_id, product_subflows_json=subflows)
        self.repository.update_job(job_id, status="approved", current_stage="approved", stage_progress=100)
        self._enqueue(job_id)
        return self.get_job(job_id)

    def cancel(self, job_id: str) -> dict[str, Any]:
        job = self.repository.get_job(job_id, internal=True)
        if job["status"] in {"completed", "completed_with_failures", "failed", "cancelled"}:
            return self.repository.get_job(job_id)
        self.repository.update_job(job_id, cancel_requested=True, status="cancelling")
        return self.repository.get_job(job_id)

    def media_path(self, media_id: str) -> Path:
        path = self.repository.media_path(media_id)
        try:
            path.relative_to(self.output_root)
        except ValueError as exc:
            raise PermissionError("Modular production media is outside the configured output root") from exc
        if not path.is_file() or path.stat().st_size <= 0:
            raise FileNotFoundError("Modular production media is missing")
        return path

    def process_pending_once(self) -> bool:
        pending = [
            job_id for job_id in self.repository.resumable_ids()
            if self.repository.get_job(job_id, internal=True)["status"] != "awaiting_review"
        ]
        if not pending:
            return False
        self._process_job(pending[0])
        return True

    def _enqueue(self, job_id: str) -> None:
        with self._guard:
            if job_id in self._queued:
                return
            self._queued.add(job_id)
        self._tasks.put(job_id)

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            job_id = self._tasks.get()
            if job_id is None:
                return
            with self._guard:
                self._queued.discard(job_id)
            try:
                self._process_job(job_id)
            except Exception as exc:
                try:
                    self.repository.update_job(
                        job_id, status="failed", current_stage="failed", error_message=str(exc),
                        completed_at=utc_now(), stage_progress=100,
                    )
                except Exception:
                    pass

    def _process_job(self, job_id: str) -> None:
        while not self._stop.is_set():
            job = self.repository.get_job(job_id, internal=True)
            if job["status"] in {"completed", "completed_with_failures", "failed", "cancelled", "awaiting_review"}:
                return
            if job["cancel_requested"]:
                self.repository.cancel_remaining(job_id)
                self.repository.update_job(
                    job_id, status="cancelled", current_stage="cancelled", completed_at=utc_now(), stage_progress=100,
                )
                return
            stage = job["current_stage"]
            if stage == "planning":
                self._stage_planning(job)
            elif stage == "approved":
                self._stage_create_renderer(job)
            elif stage in {"rendering_bases", "waiting_for_production"}:
                self._stage_rendering(job)
            elif stage == "generating_variants":
                self._stage_variants(job)
            elif stage == "compliance":
                self._stage_compliance(job)
            elif stage == "scoring":
                self._stage_scoring(job)
            elif stage == "exporting":
                self._stage_export(job)
            else:
                raise RuntimeError(f"Unsupported modular production stage: {stage}")

    def _stage_planning(self, job: dict[str, Any]) -> None:
        started = time.perf_counter()
        settings = job["settings"]
        subflows = dict(job["product_subflows"])
        warnings: list[dict[str, Any]] = []
        generated_total = 0
        for index, product in enumerate(job["product_allocation"]):
            subflow = dict(subflows[product])
            if not subflow.get("planner_run_id"):
                planner_seed = settings.get("seed")
                request = ModularPlannerRunCreateRequest(
                    production_method="modular_video", product=product,
                    requested_count=subflow["requested_base_count"],
                    requested_template=settings["requested_template"], cta_mode=settings["cta_mode"],
                    target_min_duration=settings["target_min_duration"],
                    target_max_duration=settings["target_max_duration"],
                    ingredient_shortage_policy=settings["ingredient_shortage_policy"],
                    seed=f"{planner_seed}:{product}" if planner_seed else None,
                )
                run = self.planner.create_run(request)
                product_warnings = list(run.get("warnings", []))
                if run["shortfall"]:
                    shortage = {
                        "code": "planner_shortfall", "product": product,
                        "requested": run["requested_count"], "generated": run["generated_count"],
                        "shortfall": run["shortfall"],
                    }
                    product_warnings.append(shortage)
                    warnings.append(shortage)
                subflow.update(
                    planner_run_id=run["planner_run_id"], generated_base_count=run["generated_count"],
                    status="planned", warnings=product_warnings,
                )
                subflows[product] = subflow
                self.repository.update_job(
                    job["job_id"], product_subflows_json=subflows,
                    planner_run_id=run["planner_run_id"] if len(subflows) == 1 else None,
                    stage_progress=100 * (index + 1) / len(subflows),
                )
            else:
                run = self.planner.get_run(subflow["planner_run_id"])
            generated_total += int(run["generated_count"])
            warnings.extend(row for row in subflow.get("warnings", []) if row not in warnings)
        self.repository.update_job(
            job["job_id"], product_subflows_json=subflows,
            generated_base_count=generated_total, warnings_json=warnings, stage_progress=100,
        )
        if generated_total < 1:
            raise RuntimeError("Planner generated no valid compositions")
        if job["workflow_mode"] == "review_first":
            self._finish_timing(job["job_id"], "planning", started)
            self.repository.update_job(
                job["job_id"], status="awaiting_review", current_stage="awaiting_review", stage_progress=100,
            )
            return
        for product, subflow in subflows.items():
            if int(subflow.get("generated_base_count") or 0) < 1:
                subflows[product] = {**subflow, "status": "planner_shortfall"}
                continue
            run = self.planner.get_run(subflow["planner_run_id"])
            if run["status"] == "draft":
                run = self.planner.approve(run["planner_run_id"], int(run["revision"]))
            manifest = self.planner.manifest(run["planner_run_id"], public=False)
            self._record_approval(job["job_id"], product, manifest)
            actual = len(manifest["payload"].get("compositions", []))
            subflows[product] = {
                **subflow, "planner_manifest_id": manifest["manifest_id"], "status": "approved",
                "generated_base_count": actual,
            }
        self.repository.update_job(job["job_id"], product_subflows_json=subflows)
        self._finish_timing(job["job_id"], "planning", started)
        self.repository.update_job(job["job_id"], status="approved", current_stage="approved", stage_progress=100)

    def _record_approval(self, job_id: str, product: str, manifest: dict[str, Any]) -> None:
        payload = manifest["payload"]
        job = self.repository.get_job(job_id, internal=True)
        product_offset = sum(
            count for name, count in job["product_allocation"].items()
            if list(job["product_allocation"]).index(name) < list(job["product_allocation"]).index(product)
        )
        for composition in payload.get("compositions", []):
            self.repository.upsert_item(
                job_id, composition["composition_id"], ordinal=product_offset + int(composition["ordinal"]),
                product=product, planner_run_id=payload["planner_run_id"],
                planner_manifest_id=manifest["manifest_id"],
            )
        generated = len(self.repository.get_job(job_id, internal=True)["items"])
        self.repository.update_job(
            job_id,
            planner_manifest_id=manifest["manifest_id"] if len(job["product_subflows"]) == 1 else None,
            generated_base_count=generated,
            expected_variant_count=generated * job["variants_per_base"],
        )

    def _stage_create_renderer(self, job: dict[str, Any]) -> None:
        subflows = dict(job["product_subflows"])
        for product, subflow in subflows.items():
            if subflow.get("render_run_id") or not subflow.get("generated_base_count"):
                continue
            manifest = self.planner.manifest(subflow["planner_run_id"], public=False)
            composition_ids = [row["composition_id"] for row in manifest["payload"].get("compositions", [])]
            run, _reused = self.renderer.create_run(ModularRenderRunCreateRequest(
                planner_run_id=subflow["planner_run_id"], composition_ids=composition_ids,
            ))
            subflows[product] = {**subflow, "render_run_id": run["render_run_id"], "status": "rendering_bases"}
            for composition_id in composition_ids:
                self.repository.update_item(job["job_id"], composition_id, render_run_id=run["render_run_id"])
            self.repository.update_job(
                job["job_id"], product_subflows_json=subflows,
                render_run_id=run["render_run_id"] if len(subflows) == 1 else None,
            )
        self.repository.update_job(
            job["job_id"], status="rendering_bases", current_stage="rendering_bases", stage_progress=0,
        )

    def _stage_rendering(self, job: dict[str, Any]) -> None:
        started = time.perf_counter()
        while not self._stop.is_set():
            completed = 0
            failed = 0
            terminal = True
            waiting = False
            subflows = dict(job["product_subflows"])
            for product, subflow in subflows.items():
                if not subflow.get("render_run_id"):
                    continue
                run = self.renderer.get_run(subflow["render_run_id"])
                product_completed = product_failed = 0
                for item in run.get("items", []):
                    state = item["status"]
                    product_completed += int(state == "completed")
                    product_failed += int(state == "failed")
                    self.repository.update_item(
                        job["job_id"], item["composition_id"],
                        render_item_id=f"{run['render_run_id']}:{item['composition_id']}", render_status=state,
                        error_message=item.get("error_message"),
                    )
                completed += product_completed
                failed += product_failed
                terminal = terminal and run["status"] in {"completed", "partial_failure", "failed"}
                waiting = waiting or run["status"] == "waiting_for_production"
                subflows[product] = {
                    **subflow, "rendered_base_count": product_completed, "failed_base_count": product_failed,
                    "status": run["status"],
                }
            requested = max(1, sum(job["product_allocation"].values()))
            current_stage = "waiting_for_production" if waiting else "rendering_bases"
            self.repository.update_job(
                job["job_id"], status=current_stage, current_stage=current_stage,
                rendered_base_count=completed, failed_base_count=failed,
                expected_variant_count=completed * job["variants_per_base"],
                stage_progress=100 * (completed + failed) / requested, product_subflows_json=subflows,
            )
            if terminal:
                if completed < 1:
                    raise RuntimeError("All modular base renders failed")
                self._finish_timing(job["job_id"], "rendering_bases", started)
                self.repository.update_job(
                    job["job_id"], status="generating_variants", current_stage="generating_variants", stage_progress=0,
                    modular_variant_run_id=job["job_id"],
                )
                return
            if self.repository.get_job(job["job_id"], internal=True)["cancel_requested"]:
                return
            self._stop.wait(self.poll_seconds)

    def _stage_variants(self, job: dict[str, Any]) -> None:
        started = time.perf_counter()
        compositions: dict[str, dict[str, Any]] = {}
        for subflow in job["product_subflows"].values():
            if subflow.get("planner_run_id"):
                manifest = self.planner.manifest(subflow["planner_run_id"], public=False)["payload"]
                compositions.update({row["composition_id"]: row for row in manifest.get("compositions", [])})
        internal = self.repository.get_job(job["job_id"], internal=True)
        output_root = Path(internal["output_directory"])
        transcript_words: list[dict[str, Any]] = []
        base_offsets: dict[str, float] = {}
        cursor = 0.0
        completed_bases = [item for item in internal["items"] if item["render_status"] == "completed"]
        for item_index, item in enumerate(completed_bases):
            if self.repository.get_job(job["job_id"], internal=True)["cancel_requested"]:
                return
            waited = False
            while self.standard_production_active() and not self._stop.is_set():
                waited = True
                self.repository.update_job(
                    job["job_id"], status="waiting_for_production",
                    current_stage="generating_variants", stage_progress=100 * item_index / max(1, len(completed_bases)),
                )
                if self._stop.wait(self.poll_seconds):
                    return
            if waited:
                self.repository.update_job(
                    job["job_id"], status="generating_variants", current_stage="generating_variants",
                )
            composition_id = item["composition_id"]
            render_run_id = item.get("render_run_id") or job.get("render_run_id")
            adapted = self.variants.adapter.adapt(render_run_id, composition_id)
            base_offsets[composition_id] = cursor
            for word in adapted["transcript_words"]:
                shifted = dict(word)
                shifted["start"] = round(float(word.get("start") or 0) + cursor, 6)
                shifted["end"] = round(float(word.get("end") or word.get("start") or 0) + cursor, 6)
                transcript_words.append(shifted)
            cursor += max(adapted["rendered_duration"], max((float(w.get("end") or 0) for w in adapted["transcript_words"]), default=0)) + 1.0
            existing = {row["variant_index"] for row in item.get("variants", []) if row["status"] == "completed"}
            if len(existing) == job["variants_per_base"]:
                continue
            self.repository.update_item(
                job["job_id"], composition_id, variant_status="generating",
                transcript_bridge_version=BRIDGE_VERSION,
                transcript_diagnostics_json=adapted["transcript_diagnostics"], base_identity=adapted["base_identity"],
            )
            lineage_base = self._lineage_base(job, adapted, compositions[composition_id])

            def persist(row: dict[str, Any]) -> None:
                lineage = {
                    **lineage_base,
                    "variant_profile_id": job["variant_profile_id"],
                    "variant_profile_revision": job["variant_profile_revision"],
                    "variant_index": row["variant_index"],
                    "variant_id": row["variant_id"],
                    "variant_name": row["variant_name"],
                }
                self.repository.upsert_variant({
                    **row, "job_id": job["job_id"], "composition_id": composition_id,
                    "status": "completed", "lineage_json": lineage,
                })
                from clipper_app.storage.models import LifecycleClass
                from clipper_app.storage.publishing import quick_content_identity
                from clipper_app.storage.registry import ArtifactRegistry

                produced_path = Path(str(row["output_path"])).resolve(strict=True)
                content_identity = quick_content_identity(produced_path)
                ArtifactRegistry.from_working_dir(getattr(self.cfg, "WORKING_DIR", "working")).register_artifact(
                    artifact_id=f"clip_media_{row['media_id']}",
                    artifact_type="MODULAR_VARIANT",
                    canonical_path=produced_path,
                    fingerprint=content_identity,
                    content_identity=content_identity,
                    owner_identity=str(row["media_id"]),
                    lifecycle_class=LifecycleClass.PENDING,
                    pinned=True,
                    pin_reason="modular_production_pending_scoring",
                    regeneration_evidence={"lineage": lineage},
                )

            try:
                rows = self.variants.generate(
                    adapted, output_root, job["variant_profile"],
                    production_job_id=job["job_id"], on_variant=persist,
                )
                self.repository.update_item(
                    job["job_id"], composition_id, variant_status="completed",
                    produced_variant_count=len(rows), failed_variant_count=max(0, job["variants_per_base"] - len(rows)),
                )
            except Exception as exc:
                refreshed = self.repository.get_job(job["job_id"], internal=True)
                refreshed_item = next(row for row in refreshed["items"] if row["composition_id"] == composition_id)
                produced = len([row for row in refreshed_item["variants"] if row["status"] == "completed"])
                self.repository.update_item(
                    job["job_id"], composition_id, variant_status="failed", produced_variant_count=produced,
                    failed_variant_count=max(1, job["variants_per_base"] - produced), error_message=str(exc),
                )
            refreshed = self.repository.get_job(job["job_id"], internal=True)
            generated = sum(row["produced_variant_count"] for row in refreshed["items"])
            failed = sum(row["failed_variant_count"] for row in refreshed["items"])
            subflows = dict(refreshed["product_subflows"])
            product = item.get("product") or job["product"]
            product_items = [row for row in refreshed["items"] if (row.get("product") or job["product"]) == product]
            subflows[product] = {
                **subflows[product],
                "generated_variant_count": sum(row["produced_variant_count"] for row in product_items),
                "failed_variant_count": sum(row["failed_variant_count"] for row in product_items),
            }
            self.repository.update_job(
                job["job_id"], generated_variant_count=generated, failed_variant_count=failed,
                stage_progress=100 * (item_index + 1) / max(1, len(completed_bases)),
                product_subflows_json=subflows,
            )
        refreshed = self.repository.get_job(job["job_id"], internal=True)
        subflows = dict(refreshed["product_subflows"])
        for product, subflow in subflows.items():
            product_items = [row for row in refreshed["items"] if (row.get("product") or job["product"]) == product]
            subflows[product] = {
                **subflow,
                "generated_variant_count": sum(row["produced_variant_count"] for row in product_items),
                "failed_variant_count": sum(row["failed_variant_count"] for row in product_items),
            }
        self.repository.update_job(job["job_id"], product_subflows_json=subflows)
        self._write_downstream_contract(refreshed, transcript_words, base_offsets)
        if refreshed["generated_variant_count"] < 1:
            raise RuntimeError("All Variants generation failed")
        self._finish_timing(job["job_id"], "generating_variants", started)
        self.repository.update_job(job["job_id"], status="compliance", current_stage="compliance", stage_progress=0)

    def _stage_compliance(self, job: dict[str, Any]) -> None:
        started = time.perf_counter()
        result = self.compliance.scan(ComplianceScanCommand(
            output_dir=job["output_directory"], working_dir=job["working_directory"], force=False,
        ))
        self._finish_timing(job["job_id"], "compliance", started)
        downstream = dict(job.get("downstream") or {})
        downstream["compliance"] = result.model_dump(mode="json")
        self.repository.update_job(
            job["job_id"], compliance_passed_count=result.passed,
            compliance_rejected_count=result.blocked, downstream_json=downstream,
            status="scoring", current_stage="scoring", stage_progress=0,
        )

    def _stage_scoring(self, job: dict[str, Any]) -> None:
        started = time.perf_counter()
        result = self.scoring.rescore(ScoringCommand(
            output_dir=job["output_directory"], working_dir=job["working_directory"],
        ))
        scores = list(result.scores)
        self._finish_timing(job["job_id"], "scoring", started)
        downstream = dict(job.get("downstream") or {})
        downstream["scoring"] = {"completed": len(scores)}
        self.repository.update_job(
            job["job_id"], scored_count=len(scores),
            scoring_failed_count=max(0, job["compliance_passed_count"] - len(scores)),
            downstream_json=downstream, status="exporting", current_stage="exporting", stage_progress=0,
        )

    def _stage_export(self, job: dict[str, Any]) -> None:
        started = time.perf_counter()
        manifest_path = Path(job["output_directory"]) / "manifest.json"
        rows = json.loads(manifest_path.read_text(encoding="utf-8"))
        exported = 0
        media_paths: dict[str, str] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            relative = str(row.get("output_file") or "")
            path = (Path(job["output_directory"]) / relative).resolve(strict=False)
            if bool(row.get("scorer_exported", False)) and not row.get("compliance_blocked") and path.is_file():
                exported += 1
            if row.get("media_id") and path.is_file():
                media_paths[str(row["media_id"])] = str(path)
        internal = self.repository.get_job(job["job_id"], internal=True)
        for item in internal["items"]:
            for variant in item["variants"]:
                new_path = media_paths.get(variant["media_id"])
                if new_path:
                    self.repository.upsert_variant({**variant, "output_path": new_path, "lineage_json": variant["lineage"]})
        downstream = dict(job.get("downstream") or {})
        downstream["export"] = {
            "output_manifest": "manifest.json", "normal_output_tiers": True,
            "affiliate_packaging_enabled": bool(getattr(self.cfg, "EXPORT_BATCHES_ENABLED", False)),
            "affiliate_delivery_triggered": False,
        }
        # Standard scoring has already placed accepted clips into the normal output tiers.
        # Affiliate batch intake and delivery are deliberately separate operations.
        downstream["export"]["affiliate_packaging_skipped"] = "separate_delivery_boundary"
        failures = (
            int(job["failed_base_count"]) + int(job["failed_variant_count"])
            + int(job["compliance_rejected_count"]) + int(job["scoring_failed_count"])
        )
        status = "completed_with_failures" if failures or exported < int(job["generated_variant_count"]) else "completed"
        self._finish_timing(job["job_id"], "exporting", started)
        self.repository.update_job(
            job["job_id"], exported_count=exported,
            export_failed_count=max(0, int(job["compliance_passed_count"]) - exported),
            downstream_json=downstream, status=status, current_stage=status,
            stage_progress=100, completed_at=utc_now(),
        )

    def _write_downstream_contract(
        self, job: dict[str, Any], transcript_words: list[dict[str, Any]], base_offsets: dict[str, float],
    ) -> None:
        output_root = Path(job["output_directory"])
        working_root = Path(job["working_directory"])
        rows: list[dict[str, Any]] = []
        for item in job["items"]:
            offset = base_offsets.get(item["composition_id"], 0.0)
            for variant in item["variants"]:
                output_path = Path(variant["output_path"])
                try:
                    output_file = str(output_path.relative_to(output_root)).replace("\\", "/")
                except ValueError as exc:
                    raise RuntimeError("Variant output escaped the modular production output root") from exc
                clip_id = output_path.stem
                rows.append({
                    "clip_id": clip_id,
                    "base_clip_id": f"modular_{item['composition_id']}",
                    "variant_id": variant["variant_id"],
                    "variant_index": variant["variant_index"],
                    "version_dir": f"v{variant['variant_index']}",
                    "output_file": output_file,
                    "status": "ok",
                    "product": variant["lineage"]["product"],
                    "start": offset,
                    "end": offset + float(variant.get("duration") or 0),
                    "hook": "",
                    "clip_type": "modular_video",
                    "production_method": "modular_video",
                    "modular_production_job_id": job["job_id"],
                    "composition_id": item["composition_id"],
                    "media_id": variant["media_id"],
                    "lineage": variant["lineage"],
                })
        self._write_json_atomic(output_root / "manifest.json", rows)
        self._write_json_atomic(working_root / "transcript.json", {
            "schema_version": 3,
            "origin": "modular-transcript-bridge-v1",
            "words": transcript_words,
        })

    @staticmethod
    def _lineage_base(job: dict[str, Any], adapted: dict[str, Any], composition: dict[str, Any]) -> dict[str, Any]:
        transcript_dependencies = [
            dict(item["transcript"])
            for item in (adapted.get("transcript_diagnostics") or {}).get("items", [])
            if isinstance(item, dict) and isinstance(item.get("transcript"), dict)
        ]
        return {
            "modular_production_job_id": job["job_id"],
            "product": adapted["product"],
            "planner_run_id": adapted["planner_run_id"],
            "planner_manifest_id": adapted["planner_manifest_id"],
            "composition_id": composition["composition_id"],
            "render_run_id": adapted["render_run_id"],
            "render_item_id": adapted["modular_render_item_id"],
            "transcript_bridge_version": BRIDGE_VERSION,
            "scanner_version": ANALYZER_VERSION,
            "planner_version": PLANNER_VERSION,
            "renderer_version": adapted["renderer_version"],
            "source_fingerprint_chain": adapted["base_identity"],
            "sources": [{
                "position": row["position"], "role": row["role"], "segment_id": row["segment_id"],
                "scan_id": row["scan_id"], "source_id": row["source_id"],
                "start_seconds": row["start_seconds"], "end_seconds": row["end_seconds"],
            } for row in composition.get("items", [])],
            "transcript_diagnostics": adapted["transcript_diagnostics"],
            "transcript_dependencies": transcript_dependencies,
            "regeneration": {
                "status": "METADATA_COMPLETE_DEPENDENCIES_UNVERIFIED" if transcript_dependencies else "UNVERIFIED",
                "recipe_revision": "modular-production-lineage-v1",
                "required_components": [
                    "source_fingerprint_chain", "planner_manifest_id", "renderer_version",
                    "variant_profile_revision", "transcript_dependencies",
                ],
                "missing_or_unverified": [] if transcript_dependencies else ["transcript_dependencies"],
            },
        }

    def _finish_timing(self, job_id: str, stage: str, started: float) -> None:
        job = self.repository.get_job(job_id, internal=True)
        timings = dict(job.get("timings") or {})
        timings[stage] = round(float(timings.get(stage) or 0) + time.perf_counter() - started, 3)
        self.repository.update_job(job_id, timings_json=timings)

    @staticmethod
    def _write_json_atomic(path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".partial")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    @staticmethod
    def _json_hash(payload: Any) -> str:
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
