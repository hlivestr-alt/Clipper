from __future__ import annotations

import hashlib
import json
import secrets
import time
import uuid
from pathlib import Path
from typing import Any

from clipper_app.contracts.modular_planner_models import (
    ModularPlannerRunCreateRequest,
    SUGGESTED_DURATION_DEFAULTS,
)
from clipper_app.modular_planner.library_reader import ScannerLibraryReader
from clipper_app.modular_planner.quality import joinability_inventory
from clipper_app.modular_planner.repository import PlannerRepository, utc_now
from clipper_app.modular_planner.selection import (
    PLANNER_VERSION,
    ModularPlannerSelector,
    effectively_same_timeline,
)


class PlannerConflictError(RuntimeError):
    pass


class ModularPlannerService:
    def __init__(
        self,
        cfg: Any,
        *,
        library: ScannerLibraryReader | None = None,
        repository: PlannerRepository | None = None,
        selector: ModularPlannerSelector | None = None,
    ):
        working = Path(str(getattr(cfg, "WORKING_DIR", "working") or "working"))
        vod_root = Path(str(getattr(cfg, "QUEUE_INPUT_DIR", r"D:\VOD") or r"D:\VOD"))
        self.library = library or ScannerLibraryReader(working / "modular_library.sqlite3", vod_root)
        self.repository = repository or PlannerRepository(working / "modular_planner.sqlite3")
        self.selector = selector or ModularPlannerSelector()

    def inventory(self, product: str) -> dict[str, Any]:
        inventory = self.library.inventory(product)
        quality = joinability_inventory(inventory["segments"])
        for role, counts in quality.items():
            if role in inventory["roles"]:
                inventory["roles"][role]["joinability"] = counts
        inventory.pop("segments", None)
        inventory["suggested_durations"] = [
            {"template": template, "cta_mode": cta_mode, "minimum": values[0], "maximum": values[1]}
            for (template, cta_mode), values in SUGGESTED_DURATION_DEFAULTS.items()
        ]
        return inventory

    def create_run(self, request: ModularPlannerRunCreateRequest) -> dict[str, Any]:
        started = time.perf_counter()
        values = request.model_dump(mode="json")
        inventory = self.library.inventory(values["product"])
        segments = inventory["segments"]
        seed = values.get("seed") or secrets.token_hex(16)
        run_id = uuid.uuid4().hex
        self.repository.create_run({
            **values,
            "planner_run_id": run_id,
            "seed": seed,
            "planner_version": PLANNER_VERSION,
            "inventory_snapshot_hash": inventory["snapshot_hash"],
        })
        approved_usage = self.repository.approved_usage([item["segment_id"] for item in segments])
        comparisons = self.repository.comparison_compositions(run_id)
        compositions, warnings, statistics = self.selector.generate(
            segments=segments,
            product=values["product"],
            requested_template=values["requested_template"],
            actual_template=values["requested_template"],
            cta_mode=values["cta_mode"],
            target_min_duration=values["target_min_duration"],
            target_max_duration=values["target_max_duration"],
            requested_count=values["requested_count"],
            starting_ordinal=1,
            seed=seed,
            approved_usage=approved_usage,
            current_run_usage={},
            comparisons=comparisons,
        )
        for composition in compositions:
            self.repository.add_composition(run_id, composition)

        if (
            values["requested_template"] == "ingredient"
            and values["ingredient_shortage_policy"] == "fallback_to_standard"
            and len(compositions) < values["requested_count"]
        ):
            fallback_count = values["requested_count"] - len(compositions)
            fallback, fallback_warnings, fallback_stats = self.selector.generate(
                segments=segments,
                product=values["product"],
                requested_template="ingredient",
                actual_template="standard",
                cta_mode=values["cta_mode"],
                target_min_duration=values["target_min_duration"],
                target_max_duration=values["target_max_duration"],
                requested_count=fallback_count,
                starting_ordinal=len(compositions) + 1,
                seed=seed,
                approved_usage=approved_usage,
                current_run_usage=self.repository.current_run_usage(run_id),
                comparisons=self.repository.comparison_compositions(run_id),
                fallback_reason="ingredient_inventory_exhausted",
            )
            for composition in fallback:
                self.repository.add_composition(run_id, composition)
            warnings.extend(fallback_warnings)
            statistics["fallback"] = fallback_stats

        statistics["planner_core_seconds"] = time.perf_counter() - started
        self.repository.finish_generation(run_id, warnings, statistics)
        return self._public_run(self.repository.get_run(run_id))

    def get_run(self, run_id: str) -> dict[str, Any]:
        return self._public_run(self.repository.get_run(run_id))

    def list_runs(self, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if status not in {None, "draft", "approved"}:
            raise ValueError("Invalid planner run status")
        return [self._public_run(run) for run in self.repository.list_runs(status, limit)]

    def regenerate(self, run_id: str, composition_id: str, expected_revision: int) -> dict[str, Any]:
        run = self.repository.get_run(run_id)
        if run["status"] != "draft":
            raise PlannerConflictError("Approved planner runs are immutable")
        if int(run["revision"]) != expected_revision:
            raise PlannerConflictError("Planner run revision conflict")
        original = next(
            (item for item in run["compositions"] if item["composition_id"] == composition_id), None,
        )
        if original is None:
            raise KeyError("Unknown composition")
        if original["status"] != "draft":
            raise PlannerConflictError("Only an active draft composition can be regenerated")

        inventory = self.library.inventory(run["product"])
        segments = inventory["segments"]
        approved_usage = self.repository.approved_usage([item["segment_id"] for item in segments])
        replacements, warnings, _stats = self.selector.generate(
            segments=segments,
            product=run["product"],
            requested_template=original["requested_template"],
            actual_template=original["actual_template"],
            cta_mode=original["cta_mode"],
            target_min_duration=float(original["target_min_duration"]),
            target_max_duration=float(original["target_max_duration"]),
            requested_count=1,
            starting_ordinal=int(original["ordinal"]),
            seed=run["seed"],
            approved_usage=approved_usage,
            current_run_usage=self.repository.current_run_usage(run_id),
            comparisons=self.repository.comparison_compositions(run_id),
            fallback_reason=original.get("fallback_reason"),
        )
        if not replacements:
            detail = warnings[0]["code"] if warnings else "search_exhausted"
            raise PlannerConflictError(f"No replacement composition is available: {detail}")
        self.repository.add_composition(
            run_id,
            replacements[0],
            supersedes_id=composition_id,
            expected_revision=expected_revision,
        )
        return self._public_run(self.repository.get_run(run_id))

    def remove(self, run_id: str, composition_id: str, expected_revision: int) -> dict[str, Any]:
        try:
            self.repository.remove_composition(run_id, composition_id, expected_revision)
        except RuntimeError as exc:
            raise PlannerConflictError(str(exc)) from exc
        return self._public_run(self.repository.get_run(run_id))

    def approve(self, run_id: str, expected_revision: int) -> dict[str, Any]:
        run = self.repository.get_run(run_id)
        if run["status"] != "draft" or int(run["revision"]) != expected_revision:
            raise PlannerConflictError("Planner run is immutable or its revision is stale")
        active = [item for item in run["compositions"] if item["status"] == "draft"]
        if not active:
            raise ValueError("At least one active draft composition is required")

        source_failures: list[dict[str, str]] = []
        checked: set[str] = set()
        for composition in active:
            expected_duration = sum(float(item["duration_seconds"]) for item in composition["items"])
            if abs(expected_duration - float(composition["actual_duration"])) > 0.001:
                raise PlannerConflictError(f"Composition {composition['composition_id']} duration snapshot is inconsistent")
            if not float(composition["target_min_duration"]) <= expected_duration <= float(composition["target_max_duration"]):
                raise PlannerConflictError(f"Composition {composition['composition_id']} is outside its target range")
            for item in composition["items"]:
                if item["source_id"] in checked:
                    continue
                checked.add(item["source_id"])
                reason = self.library.verify_source(item)
                if reason:
                    source_failures.append({
                        "source_id": item["source_id"], "source_filename": item["source_filename"], "reason": reason,
                    })
        if source_failures:
            raise PlannerConflictError(f"Source verification failed: {json.dumps(source_failures)}")

        approved_elsewhere = [
            item for item in self.repository.comparison_compositions(run_id)
            if item["status"] == "approved" and item["planner_run_id"] != run_id
        ]
        for composition in active:
            if any(effectively_same_timeline(composition["items"], prior["items"]) for prior in approved_elsewhere):
                raise PlannerConflictError("An effectively identical timeline has already been approved")

        manifest = self._manifest_payload(run, active)
        encoded = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        checksum = hashlib.sha256(encoded).hexdigest()
        try:
            self.repository.approve(run_id, expected_revision, manifest, checksum)
        except RuntimeError as exc:
            raise PlannerConflictError(str(exc)) from exc
        return self._public_run(self.repository.get_run(run_id))

    def manifest(self, run_id: str, *, public: bool = True) -> dict[str, Any]:
        manifest = self.repository.manifest(run_id)
        if not public:
            return manifest
        payload = json.loads(json.dumps(manifest["payload"]))
        for composition in payload.get("compositions", []):
            for item in composition.get("items", []):
                item.pop("canonical_path", None)
        return {**manifest, "payload": payload}

    @staticmethod
    def _public_run(run: dict[str, Any]) -> dict[str, Any]:
        payload = json.loads(json.dumps(run))
        for composition in payload.get("compositions", []):
            for item in composition.get("items", []):
                item.pop("canonical_path", None)
        return payload

    @staticmethod
    def _manifest_payload(run: dict[str, Any], active: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "planner_run_id": run["planner_run_id"],
            "production_method": "modular_video",
            "planner_version": run["planner_version"],
            "seed": run["seed"],
            "approved_at": utc_now(),
            "product": run["product"],
            "requested_count": run["requested_count"],
            "generated_count": len(active),
            "requested_template": run["requested_template"],
            "ingredient_shortage_policy": run["ingredient_shortage_policy"],
            "cta_mode": run["cta_mode"],
            "target_min_duration": run["target_min_duration"],
            "target_max_duration": run["target_max_duration"],
            "warnings": run["warnings"],
            "search_statistics": run["search_statistics"],
            "compositions": [
                {
                    "composition_id": composition["composition_id"],
                    "ordinal": composition["ordinal"],
                    "requested_template": composition["requested_template"],
                    "actual_template": composition["actual_template"],
                    "fallback_reason": composition["fallback_reason"],
                    "cta_mode": composition["cta_mode"],
                    "target_min_duration": composition["target_min_duration"],
                    "target_max_duration": composition["target_max_duration"],
                    "actual_duration": composition["actual_duration"],
                    "distinct_source_count": composition["distinct_source_count"],
                    "exact_signature": composition["exact_signature"],
                    "near_signature": composition["near_signature"],
                    "signature_version": composition["signature_version"],
                    "selection_score": composition["selection_score"],
                    "selection_metadata": composition["selection_metadata"],
                    "items": composition["items"],
                }
                for composition in active
            ],
        }
