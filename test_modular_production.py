from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from clipper_app.contracts.modular_production_models import (
    ModularProductionContinueRequest,
    ModularProductionJobCreateRequest,
)
from clipper_app.contracts.models import ComplianceScanResult, ScoringResult
from clipper_app.modular_production import ModularProductionRepository, ModularProductionService
from clipper_app.modular_production.service import CANONICAL_PRODUCTS, allocate_products


class FakePlanner:
    def __init__(self):
        self.runs = {}
        self.created = 0
        self.shortfall_by_product = {}

    def create_run(self, request):
        self.created += 1
        run_id = f"planner-{self.created}"
        generated_count = max(0, request.requested_count - self.shortfall_by_product.get(str(request.product), 0))
        compositions = [self._composition(run_id, index + 1) for index in range(generated_count)]
        self.runs[run_id] = {
            "planner_run_id": run_id, "status": "draft", "revision": 1,
            "product": str(request.product),
            "requested_count": request.requested_count, "generated_count": len(compositions),
            "shortfall": request.requested_count - generated_count, "warnings": [], "compositions": compositions,
        }
        return dict(self.runs[run_id])

    def get_run(self, run_id):
        return dict(self.runs[run_id])

    def approve(self, run_id, revision):
        run = self.runs[run_id]
        if run["status"] != "draft" or run["revision"] != revision:
            raise RuntimeError("stale")
        run["status"] = "approved"
        run["revision"] += 1
        return dict(run)

    def remove(self, run_id, composition_id, expected_revision):
        run = self.runs[run_id]
        self._revision(run, expected_revision)
        for row in run["compositions"]:
            if row["composition_id"] == composition_id:
                row["status"] = "removed"
        run["generated_count"] = len([row for row in run["compositions"] if row["status"] == "draft"])
        return dict(run)

    def regenerate(self, run_id, composition_id, expected_revision):
        run = self.runs[run_id]
        self._revision(run, expected_revision)
        old = next(row for row in run["compositions"] if row["composition_id"] == composition_id)
        old["status"] = "superseded"
        run["compositions"].append(self._composition(run_id, old["ordinal"], suffix="r"))
        return dict(run)

    def manifest(self, run_id, public=False):
        del public
        run = self.runs[run_id]
        if run["status"] != "approved":
            raise KeyError("Approved manifest was not found")
        active = [row for row in run["compositions"] if row["status"] in {"draft", "approved"}]
        return {
            "manifest_id": f"manifest-{run_id}", "checksum_sha256": "checksum",
            "payload": {"planner_run_id": run_id, "product": run["product"], "compositions": active},
        }

    @staticmethod
    def _revision(run, expected):
        if run["revision"] != expected:
            raise RuntimeError("stale")
        run["revision"] += 1

    @staticmethod
    def _composition(run_id, ordinal, suffix=""):
        composition_id = f"{run_id}-composition-{ordinal}{suffix}"
        return {
            "composition_id": composition_id, "ordinal": ordinal, "status": "draft",
            "items": [
                {"position": 0, "role": "hook", "segment_id": f"seg-h-{ordinal}", "scan_id": "scan-1", "source_id": "vod-1", "start_seconds": 10.0, "end_seconds": 20.0},
                {"position": 1, "role": "benefits", "segment_id": f"seg-b-{ordinal}", "scan_id": "scan-2", "source_id": "vod-2", "start_seconds": 30.0, "end_seconds": 50.0},
                {"position": 2, "role": "cta", "segment_id": f"seg-c-{ordinal}", "scan_id": "scan-1", "source_id": "vod-1", "start_seconds": 60.0, "end_seconds": 70.0},
            ],
        }


class FakeRenderer:
    def __init__(self, planner, failed_ordinals=()):
        self.planner = planner
        self.failed_ordinals = set(failed_ordinals)
        self.runs = {}
        self.created = 0

    def create_run(self, request):
        self.created += 1
        run_id = f"render-{self.created}"
        compositions = self.planner.manifest(request.planner_run_id)["payload"]["compositions"]
        items = []
        for row in compositions:
            failed = row["ordinal"] in self.failed_ordinals
            items.append({
                "composition_id": row["composition_id"], "ordinal": row["ordinal"],
                "status": "failed" if failed else "completed",
                "error_message": "source verification failed" if failed else None,
            })
        succeeded = sum(item["status"] == "completed" for item in items)
        failed = len(items) - succeeded
        self.runs[run_id] = {
            "render_run_id": run_id, "status": "partial_failure" if failed and succeeded else ("failed" if failed else "completed"),
            "items": items,
        }
        return dict(self.runs[run_id]), False

    def get_run(self, run_id):
        return dict(self.runs[run_id])


class FakeAdapter:
    def __init__(self, root, planner):
        self.root = root
        self.planner = planner

    def adapt(self, render_run_id, composition_id):
        base = self.root / f"{composition_id}.mp4"
        base.write_bytes(b"base")
        ordinal = int(composition_id.rstrip("r").rsplit("-", 1)[1])
        planner_run_id = composition_id.split("-composition-", 1)[0]
        product = self.planner.runs[planner_run_id]["product"]
        return {
            "render_run_id": render_run_id, "modular_render_item_id": f"{render_run_id}:{composition_id}",
            "planner_run_id": planner_run_id, "planner_manifest_id": f"manifest-{planner_run_id}",
            "composition_id": composition_id, "product": product, "renderer_version": "modular-renderer-v1.1",
            "base_path": str(base), "base_identity": f"fingerprint-{composition_id}", "ordinal": ordinal,
            "rendered_duration": 40.0, "transcript_words": [{"word": "serum", "start": 0.2, "end": 0.8}],
            "hook_text": "serum", "transcript_diagnostics": {"timing_mode": "source_word_timestamps"},
        }


class FakeVariants:
    bridge_version = "modular-transcript-bridge-v1"

    def __init__(self, root, planner, fail_ordinal=None):
        self.adapter = FakeAdapter(root, planner)
        self.fail_ordinal = fail_ordinal

    def profiles(self):
        return {"profiles": [{"profile_id": "active", "name": "Active", "revision": "profile-r1", "variant_count": 2}]}

    def freeze_profile(self, profile_id):
        return {"revision": "profile-r1", "variants": [{"name": "Original"}, {"name": "Black Bars"}]}

    def generate(self, adapted, output_directory, profile, *, production_job_id, on_variant):
        rows = []
        ordinal = adapted["ordinal"]
        for index, variant in enumerate(profile["variants"]):
            if ordinal == self.fail_ordinal and index == 1:
                raise RuntimeError("variant render failed")
            folder = Path(output_directory) / f"v{index}"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"modular_{adapted['composition_id']}_v{index}.mp4"
            path.write_bytes(b"variant")
            row = {
                "media_id": f"media-{production_job_id}-{adapted['composition_id']}-{index}",
                "variant_index": index, "variant_id": f"v{index}", "variant_name": variant["name"],
                "output_path": str(path), "duration": 40.0, "file_size": path.stat().st_size,
            }
            on_variant(row)
            rows.append(row)
        return rows


class FakeCompliance:
    def __init__(self, reject=0):
        self.reject = reject

    def scan(self, command):
        path = Path(command.output_dir) / "manifest.json"
        rows = json.loads(path.read_text(encoding="utf-8"))
        for index, row in enumerate(rows):
            blocked = index < self.reject
            row.update(compliance_passed=not blocked, compliance_blocked=blocked)
            if blocked:
                row["status"] = "compliance_blocked"
        path.write_text(json.dumps(rows), encoding="utf-8")
        return ComplianceScanResult(
            output_dir=command.output_dir, manifest_path=str(path), scanned=len(rows),
            passed=len(rows) - self.reject, blocked=self.reject,
        )


class FakeScoring:
    def rescore(self, command):
        path = Path(command.output_dir) / "manifest.json"
        rows = json.loads(path.read_text(encoding="utf-8"))
        scores = []
        for row in rows:
            if row.get("compliance_blocked"):
                continue
            row["scorer_exported"] = True
            scores.append({"clip_id": row["clip_id"], "total_score": 8.0})
        path.write_text(json.dumps(rows), encoding="utf-8")
        return ScoringResult(scores=tuple(scores))


class FakeExports:
    pass


class ModularProductionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cfg = SimpleNamespace(
            WORKING_DIR=str(self.root / "working"), OUTPUT_DIR=str(self.root / "output"),
            EXPORT_BATCHES_ENABLED=False,
        )
        self.planner = FakePlanner()

    def tearDown(self):
        self.temp.cleanup()

    def request(self, **changes):
        values = {
            "product": "serum", "requested_base_count": 2, "requested_template": "standard",
            "cta_mode": "use_cta", "target_min_duration": 45, "target_max_duration": 75,
            "ingredient_shortage_policy": "partial", "variant_profile_id": "active",
        }
        values.update(changes)
        return ModularProductionJobCreateRequest(**values)

    def service(self, *, failed_bases=(), fail_variant=None, reject=0, repository=None):
        renderer = FakeRenderer(self.planner, failed_bases)
        return ModularProductionService(
            self.cfg, planner=self.planner, renderer=renderer,
            variants=FakeVariants(self.root, self.planner, fail_variant), compliance=FakeCompliance(reject),
            scoring=FakeScoring(), exports=FakeExports(),
            repository=repository or ModularProductionRepository(self.root / "working" / "modular_production.sqlite3"),
            standard_production_active=lambda: False, start_worker=False, poll_seconds=0.01,
        )

    def test_automatic_runs_all_stages_and_freezes_normal_manifest(self):
        service = self.service()
        created, reused = service.create_job(self.request())
        self.assertFalse(reused)
        self.assertTrue(service.process_pending_once())
        job = service.get_job(created["job_id"])
        self.assertEqual(job["status"], "completed")
        self.assertEqual((job["generated_base_count"], job["rendered_base_count"]), (2, 2))
        self.assertEqual((job["expected_variant_count"], job["generated_variant_count"]), (4, 4))
        self.assertEqual((job["compliance_passed_count"], job["scored_count"], job["exported_count"]), (4, 4, 4))
        self.assertIsNotNone(job["planner_manifest_id"])
        self.assertEqual(job["variant_profile_revision"], "profile-r1")
        sample = job["items"][0]["variants"][0]["lineage"]
        self.assertEqual(sample["transcript_bridge_version"], "modular-transcript-bridge-v1")
        self.assertEqual(sample["renderer_version"], "modular-renderer-v1.1")
        self.assertEqual(sample["sources"][0]["role"], "hook")

    def test_review_first_stops_and_reuses_existing_planner_review(self):
        service = self.service()
        created, _ = service.create_job(self.request(workflow_mode="review_first"))
        service.process_pending_once()
        waiting = service.get_job(created["job_id"])
        self.assertEqual(waiting["status"], "awaiting_review")
        self.assertIsNone(waiting["render_run_id"])
        run = self.planner.get_run(waiting["planner_run_id"])
        self.planner.remove(run["planner_run_id"], run["compositions"][0]["composition_id"], run["revision"])
        revised = self.planner.get_run(run["planner_run_id"])
        continued = service.continue_job(
            created["job_id"], ModularProductionContinueRequest(expected_planner_revision=revised["revision"]),
        )
        self.assertEqual(continued["status"], "approved")
        service.process_pending_once()
        self.assertEqual(service.get_job(created["job_id"])["status"], "completed")
        self.assertEqual(service.get_job(created["job_id"])["generated_base_count"], 1)

    def test_active_start_is_idempotent_and_explicit_rerun_is_new(self):
        service = self.service()
        first, reused = service.create_job(self.request())
        second, second_reused = service.create_job(self.request())
        self.assertFalse(reused)
        self.assertTrue(second_reused)
        self.assertEqual(first["job_id"], second["job_id"])
        service.process_pending_once()
        rerun, rerun_reused = service.create_job(self.request(explicit_rerun=True))
        self.assertFalse(rerun_reused)
        self.assertNotEqual(first["job_id"], rerun["job_id"])
        self.assertEqual(rerun["rerun_of_job_id"], first["job_id"])

    def test_partial_failures_continue_and_counts_are_durable(self):
        service = self.service(failed_bases=(2,), fail_variant=1, reject=1)
        created, _ = service.create_job(self.request(requested_base_count=3))
        service.process_pending_once()
        job = service.get_job(created["job_id"])
        self.assertEqual(job["status"], "completed_with_failures")
        self.assertEqual((job["rendered_base_count"], job["failed_base_count"]), (2, 1))
        self.assertEqual(job["expected_variant_count"], 4)
        self.assertEqual((job["generated_variant_count"], job["failed_variant_count"]), (3, 1))
        self.assertEqual((job["compliance_rejected_count"], job["scored_count"], job["exported_count"]), (1, 2, 2))

    def test_restart_resumes_from_persisted_approved_stage_without_new_planner_run(self):
        repository = ModularProductionRepository(self.root / "working" / "modular_production.sqlite3")
        first = self.service(repository=repository)
        created, _ = first.create_job(self.request(workflow_mode="review_first"))
        first.process_pending_once()
        waiting = first.get_job(created["job_id"])
        first.continue_job(
            created["job_id"], ModularProductionContinueRequest(expected_planner_revision=1),
        )
        created_count = self.planner.created
        restarted = self.service(repository=repository)
        restarted.process_pending_once()
        self.assertEqual(self.planner.created, created_count)
        self.assertEqual(restarted.get_job(created["job_id"])["status"], "completed")

    def test_cooperative_cancellation_marks_unstarted_items_and_preserves_history(self):
        service = self.service()
        created, _ = service.create_job(self.request(workflow_mode="review_first"))
        service.process_pending_once()
        waiting = service.get_job(created["job_id"])
        service.continue_job(
            created["job_id"], ModularProductionContinueRequest(expected_planner_revision=1),
        )
        service.cancel(created["job_id"])
        service.process_pending_once()
        cancelled = service.get_job(created["job_id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertTrue(cancelled["completed_at"])
        self.assertTrue(all(item["render_status"] == "cancelled" for item in cancelled["items"]))
        self.assertTrue(all(item["variant_status"] == "cancelled" for item in cancelled["items"]))

    def test_all_products_distribution_is_balanced_and_stable(self):
        for count, expected_minimum in ((6, 1), (12, 2), (24, 4), (30, 5), (25, 4), (31, 5)):
            allocation = allocate_products(count, "stable-job")
            self.assertEqual(tuple(allocation), CANONICAL_PRODUCTS)
            self.assertEqual(sum(allocation.values()), count)
            self.assertEqual(min(allocation.values()), expected_minimum)
            self.assertLessEqual(max(allocation.values()) - min(allocation.values()), 1)
            self.assertEqual(allocation, allocate_products(count, "stable-job"))
        with self.assertRaises(ValueError):
            self.request(product="all_products", requested_base_count=5)

    def test_all_products_automatic_uses_six_real_planner_runs_and_canonical_lineage(self):
        service = self.service()
        created, reused = service.create_job(self.request(product="all_products", requested_base_count=6, seed="job-a"))
        self.assertFalse(reused)
        self.assertEqual(created["product_scope"], "all")
        self.assertEqual(created["product_allocation"], {product: 1 for product in CANONICAL_PRODUCTS})
        service.process_pending_once()
        job = service.get_job(created["job_id"])
        self.assertEqual(job["status"], "completed")
        self.assertEqual(self.planner.created, 6)
        self.assertEqual({run["product"] for run in self.planner.runs.values()}, set(CANONICAL_PRODUCTS))
        self.assertNotIn("all_products", {run["product"] for run in self.planner.runs.values()})
        self.assertEqual((job["generated_base_count"], job["rendered_base_count"]), (6, 6))
        self.assertEqual((job["expected_variant_count"], job["generated_variant_count"]), (12, 12))
        item_products = {item["product"] for item in job["items"]}
        lineage_products = {variant["lineage"]["product"] for item in job["items"] for variant in item["variants"]}
        self.assertEqual(item_products, set(CANONICAL_PRODUCTS))
        self.assertEqual(lineage_products, set(CANONICAL_PRODUCTS))
        self.assertNotIn("all_products", lineage_products)

    def test_all_products_review_first_groups_plans_and_approves_all(self):
        service = self.service()
        created, _ = service.create_job(self.request(
            product="all_products", requested_base_count=6, workflow_mode="review_first", seed="job-review",
        ))
        service.process_pending_once()
        waiting = service.get_job(created["job_id"])
        self.assertEqual(waiting["status"], "awaiting_review")
        self.assertEqual([plan["product"] for plan in waiting["product_plans"]], list(CANONICAL_PRODUCTS))
        self.assertTrue(all(flow["render_run_id"] is None for flow in waiting["product_subflows"].values()))
        revisions = {plan["product"]: plan["revision"] for plan in waiting["product_plans"]}
        service.continue_job(created["job_id"], ModularProductionContinueRequest(expected_planner_revisions=revisions))
        service.process_pending_once()
        self.assertEqual(service.get_job(created["job_id"])["status"], "completed")

    def test_all_products_allocation_and_subflow_ids_survive_restart(self):
        repository = ModularProductionRepository(self.root / "working" / "modular_production.sqlite3")
        first = self.service(repository=repository)
        created, _ = first.create_job(self.request(
            product="all_products", requested_base_count=25, workflow_mode="review_first", seed="frozen",
        ))
        first.process_pending_once()
        frozen = first.get_job(created["job_id"])
        created_count = self.planner.created
        restarted = self.service(repository=repository)
        recovered = restarted.get_job(created["job_id"])
        self.assertEqual(recovered["product_allocation"], frozen["product_allocation"])
        self.assertEqual(recovered["product_subflows"], frozen["product_subflows"])
        self.assertEqual(self.planner.created, created_count)

    def test_all_products_planner_shortfall_stays_with_its_product(self):
        self.planner.shortfall_by_product["eye_cream"] = 1
        service = self.service()
        created, _ = service.create_job(self.request(product="all_products", requested_base_count=12, seed="shortfall"))
        service.process_pending_once()
        job = service.get_job(created["job_id"])
        self.assertEqual(job["product_allocation"], {product: 2 for product in CANONICAL_PRODUCTS})
        self.assertEqual(job["generated_base_count"], 11)
        self.assertEqual(job["product_subflows"]["eye_cream"]["generated_base_count"], 1)
        self.assertTrue(all(
            flow["generated_base_count"] == 2 for product, flow in job["product_subflows"].items()
            if product != "eye_cream"
        ))
        warning = next(row for row in job["warnings"] if row["code"] == "planner_shortfall")
        self.assertEqual((warning["product"], warning["shortfall"]), ("eye_cream", 1))


if __name__ == "__main__":
    unittest.main()
