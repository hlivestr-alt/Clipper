import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import module_visual_validator as mvv
from module_review import update_module_review


class VisualCfg:
    MODULE_VISUAL_VALIDATION_MIN_HITS = 1
    MODULE_VISUAL_VALIDATION_MIN_CONFIDENCE = 0.55
    MODULE_VISUAL_VALIDATION_SAMPLE_FPS = 1.0
    YOLO_WEIGHTS = "models/proya_best.pt"
    YOLO_IMGSZ = 416
    YOLO_HALF = True
    PRODUCT_CLASSES = {0: "cleanser", 1: "serum", 2: "toner"}
    MODULE_INDEX_VALIDATE_MEDIA = False
    MODULE_INDEX_LOCK_TIMEOUT = 0.1
    MODULE_FILE_LOCK_TIMEOUT = 0.1
    MODULE_WORD_FALLBACK_REVIEW_REQUIRED = True
    MODULE_PRODUCT_EVIDENCE_REQUIRED = False
    MODULE_ASSEMBLY_REQUIRE_APPROVED = True
    MODULE_ASSEMBLY_SAME_DATE_ONLY = False
    MODULE_ASSEMBLY_CANDIDATE_POOL = 30
    MODULAR_ASSEMBLY_READY_MIN_HOOK = 1
    MODULAR_ASSEMBLY_READY_MIN_MAIN = 1
    MODULAR_ASSEMBLY_READY_MIN_CTA = 1
    MODULE_ASSEMBLY_MIN_SOURCE_VIDEOS = 1


def module_record(
    root: Path,
    quality_status: str = "approved",
    product: str = "serum",
    role: str = "hook",
    module_id: str | None = None,
    confidence: float = 0.9,
) -> tuple[Path, dict]:
    module_id = module_id or f"{product}_{role}_20260509_0"
    media = root / product / role / f"{module_id}.mp4"
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(b"video")
    record = {
        "schema_version": 2,
        "module_id": module_id,
        "product": product,
        "role": role,
        "source_video": "vod.mp4",
        "file_path": str(media),
        "start": 0.0,
        "end": 30.0 if role == "main" else 6.0,
        "duration": 30.0 if role == "main" else 6.0,
        "confidence": confidence,
        "quality_status": quality_status,
        "quality_reason": "sentence_boundary_validated",
        "review_status": "pending",
        "boundary_mode": "sentence",
        "transcript_text": f"{product} sample text akhir.",
        "words": [{"word": product, "start": 0.0, "end": 0.4}, {"word": "akhir.", "start": 1.0, "end": 1.4}],
    }
    media.with_suffix(".json").write_text(json.dumps(record), encoding="utf-8")
    return media, record


class ModuleVisualValidatorTests(unittest.TestCase):
    def test_visual_validation_pass_writes_fields_and_keeps_quality(self):
        with tempfile.TemporaryDirectory() as tmp:
            media, record = module_record(Path(tmp))
            event = {
                "product": "serum",
                "class_name": "serum",
                "relative_start": 0.5,
                "relative_end": 1.0,
                "best_confidence": 0.91,
            }
            with mock.patch.object(
                mvv,
                "scan_module_visual_events",
                return_value={"hits": 2, "confidence_max": 0.91, "events": [event]},
            ):
                updated = mvv.validate_module_record_visual(record, VisualCfg)

        self.assertEqual(updated["visual_validation_status"], "passed")
        self.assertEqual(updated["visual_product_hits"], 2)
        self.assertEqual(updated["visual_product_events"], [event])
        self.assertEqual(updated["quality_status"], "approved")

    def test_visual_validation_failure_marks_needs_review_not_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            _media, record = module_record(Path(tmp))
            with mock.patch.object(
                mvv,
                "scan_module_visual_events",
                return_value={"hits": 0, "confidence_max": 0.0, "events": []},
            ):
                updated = mvv.validate_module_record_visual(record, VisualCfg)

        self.assertEqual(updated["visual_validation_status"], "failed")
        self.assertEqual(updated["quality_status"], "needs_review")
        self.assertEqual(updated["quality_reason"], "visual_product_mismatch")
        self.assertEqual(updated["review_status"], "pending")

    def test_visual_validation_failure_preserves_manual_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            _media, record = module_record(Path(tmp))
            record["review_status"] = "approved"
            record["quality_status"] = "approved"
            record["quality_reason"] = "manual_review_approved"
            with mock.patch.object(
                mvv,
                "scan_module_visual_events",
                return_value={"hits": 0, "confidence_max": 0.0, "events": []},
            ):
                updated = mvv.validate_module_record_visual(record, VisualCfg)

        self.assertEqual(updated["visual_validation_status"], "failed")
        self.assertEqual(updated["review_status"], "approved")
        self.assertEqual(updated["quality_status"], "approved")
        self.assertEqual(updated["quality_reason"], "manual_review_approved")

    def test_visual_validation_failure_does_not_unblock_blocked_module(self):
        with tempfile.TemporaryDirectory() as tmp:
            _media, record = module_record(Path(tmp), quality_status="blocked")
            record["review_status"] = "blocked"
            record["quality_reason"] = "manual_review_blocked"
            with mock.patch.object(
                mvv,
                "scan_module_visual_events",
                return_value={"hits": 0, "confidence_max": 0.0, "events": []},
            ):
                updated = mvv.validate_module_record_visual(record, VisualCfg)

        self.assertEqual(updated["visual_validation_status"], "failed")
        self.assertEqual(updated["quality_status"], "blocked")
        self.assertEqual(updated["quality_reason"], "manual_review_blocked")

    def test_validate_library_updates_sidecar_and_index_visual_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            library = Path(tmp) / "library"
            media, _record = module_record(library)

            class Cfg(VisualCfg):
                MODULE_LIBRARY_DIR = str(library)

            with mock.patch.object(
                mvv,
                "scan_module_visual_events",
                return_value={"hits": 1, "confidence_max": 0.8, "events": []},
            ):
                result = mvv.validate_module_library_visual(Cfg, limit=1)

            sidecar = json.loads(media.with_suffix(".json").read_text(encoding="utf-8"))
            index = json.loads((library / "index.json").read_text(encoding="utf-8"))

        self.assertEqual(result["validated"], 1)
        self.assertEqual(sidecar["visual_validation_status"], "passed")
        self.assertIn("visual_validation_fingerprint", sidecar)
        self.assertEqual(index["modules"][0]["visual_validation_status"], "passed")
        self.assertNotIn("visual_product_events", index["modules"][0])

    def test_visual_library_filters_status_role_and_approved_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            library = Path(tmp) / "library"
            module_record(library, product="serum", role="hook", module_id="serum_hook_needs_review", quality_status="needs_review")
            _media, approved = module_record(library, product="serum", role="main", module_id="serum_main_failed")
            approved["visual_validation_status"] = "failed"
            Path(approved["file_path"]).with_suffix(".json").write_text(json.dumps(approved), encoding="utf-8")
            module_record(library, product="toner", role="main", module_id="toner_main_failed")

            class Cfg(VisualCfg):
                MODULE_LIBRARY_DIR = str(library)

            seen = []

            def fake_scan(module_path, product, cfg, model=None):
                seen.append(Path(module_path).stem)
                return {"hits": 1, "confidence_max": 0.8, "events": []}

            with mock.patch.object(mvv, "scan_module_visual_events", side_effect=fake_scan):
                result = mvv.validate_module_library_visual(
                    Cfg,
                    visual_status="failed",
                    role="main",
                    approved_only=True,
                    priority="index_order",
                )

        self.assertEqual(result["validated"], 1)
        self.assertEqual(seen, ["serum_main_failed"])
        self.assertGreaterEqual(result["skipped_filter"], 2)

    def test_not_run_filter_counts_current_fingerprints_without_consuming_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            library = Path(tmp) / "library"
            current_media, current = module_record(library, module_id="serum_hook_current")
            current["visual_validation_status"] = "passed"
            current["visual_validation_fingerprint"] = mvv.build_visual_validation_fingerprint(current, VisualCfg)
            current_media.with_suffix(".json").write_text(json.dumps(current), encoding="utf-8")
            module_record(library, module_id="serum_hook_pending")

            class Cfg(VisualCfg):
                MODULE_LIBRARY_DIR = str(library)

            seen = []

            def fake_scan(module_path, product, cfg, model=None):
                seen.append(Path(module_path).stem)
                return {"hits": 1, "confidence_max": 0.8, "events": []}

            with mock.patch.object(mvv, "scan_module_visual_events", side_effect=fake_scan):
                result = mvv.validate_module_library_visual(Cfg, visual_status="not_run", limit=1, priority="index_order")

        self.assertEqual(seen, ["serum_hook_pending"])
        self.assertEqual(result["validated"], 1)
        self.assertEqual(result["skipped_current"], 1)

    def test_fingerprint_current_skips_unless_forced(self):
        with tempfile.TemporaryDirectory() as tmp:
            library = Path(tmp) / "library"
            media, record = module_record(library)
            record["visual_validation_status"] = "passed"
            record["visual_validation_fingerprint"] = mvv.build_visual_validation_fingerprint(record, VisualCfg)
            media.with_suffix(".json").write_text(json.dumps(record), encoding="utf-8")

            class Cfg(VisualCfg):
                MODULE_LIBRARY_DIR = str(library)

            with mock.patch.object(mvv, "scan_module_visual_events") as scan:
                skipped = mvv.validate_module_library_visual(Cfg, visual_status="all")

            with mock.patch.object(
                mvv,
                "scan_module_visual_events",
                return_value={"hits": 1, "confidence_max": 0.8, "events": []},
            ) as forced_scan:
                forced = mvv.validate_module_library_visual(Cfg, visual_status="all", force=True)

        scan.assert_not_called()
        forced_scan.assert_called_once()
        self.assertEqual(skipped["validated"], 0)
        self.assertEqual(skipped["skipped_current"], 1)
        self.assertEqual(forced["validated"], 1)

    def test_fingerprint_current_normalizes_legacy_relative_module_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            library = Path(tmp) / "library"
            media, record = module_record(library)
            record["visual_validation_status"] = "passed"
            fingerprint = mvv.build_visual_validation_fingerprint(record, VisualCfg)
            fingerprint["module_media_identity"]["path"] = media.name.casefold()
            fingerprint["fingerprint_hash"] = mvv._fingerprint_hash(fingerprint)
            record["visual_validation_fingerprint"] = fingerprint
            media.with_suffix(".json").write_text(json.dumps(record), encoding="utf-8")

            class Cfg(VisualCfg):
                MODULE_LIBRARY_DIR = str(library)

            with mock.patch.object(mvv, "scan_module_visual_events") as scan:
                skipped = mvv.validate_module_library_visual(Cfg, visual_status="all")

        scan.assert_not_called()
        self.assertEqual(skipped["validated"], 0)
        self.assertEqual(skipped["skipped_current"], 1)

    def test_source_vod_result_uses_bulk_fingerprint_and_bulk_queue_skips_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            library = Path(tmp) / "library"
            media, record = module_record(library)
            event = {
                "source": "source_vod_visual_validation",
                "product": "serum",
                "class_name": "serum",
                "relative_start": 0.2,
                "relative_end": 0.8,
                "best_confidence": 0.88,
            }
            record = mvv.apply_source_vod_visual_result(
                record,
                {"hits": 1, "confidence_max": 0.88, "events": [event]},
                VisualCfg,
            )
            expected_fingerprint = mvv.build_visual_validation_fingerprint(record, VisualCfg)
            media.with_suffix(".json").write_text(json.dumps(record), encoding="utf-8")

            class Cfg(VisualCfg):
                MODULE_LIBRARY_DIR = str(library)

            with mock.patch.object(mvv, "scan_module_visual_events") as scan:
                skipped = mvv.validate_module_library_visual(Cfg, visual_status="all")

        scan.assert_not_called()
        self.assertEqual(record["visual_validation_mode"], "source_vod_pre_cut")
        self.assertEqual(record["visual_validation_fingerprint"], expected_fingerprint)
        self.assertEqual(skipped["validated"], 0)
        self.assertEqual(skipped["skipped_current"], 1)

    def test_source_vod_zero_events_marks_no_visual_events_not_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            _media, record = module_record(Path(tmp))
            updated = mvv.apply_source_vod_visual_result(
                record,
                {"hits": 0, "confidence_max": 0.0, "events": []},
                VisualCfg,
            )

        self.assertEqual(updated["visual_validation_status"], "failed")
        self.assertEqual(updated["visual_validation_reason"], "source_vod_no_visual_events")
        self.assertEqual(updated["quality_status"], "no_visual_events")
        self.assertEqual(updated["quality_reason"], "source_vod_no_visual_events")
        self.assertNotEqual(updated["quality_status"], "blocked")

    def test_visual_prediction_falls_back_to_cpu_after_cuda_error(self):
        class FakeModel:
            def __init__(self, fail_on_device: str | None = None):
                self.fail_on_device = fail_on_device
                self.calls = []

            def predict(self, frames, **kwargs):
                self.calls.append({"frames": frames, **kwargs})
                if kwargs.get("device") == self.fail_on_device:
                    raise RuntimeError("CUDA error: unknown error")
                return ["ok"]

        gpu_model = FakeModel(fail_on_device="0")
        cpu_model = FakeModel()

        class Cfg(VisualCfg):
            YOLO_DEVICE = "0"
            YOLO_HALF = True

        mvv._CUDA_VALIDATION_DISABLED = False
        try:
            with mock.patch.object(mvv, "_load_yolo_model", return_value=cpu_model):
                result = mvv._predict_visual_validation(gpu_model, ["frame"], Cfg, 0.55, context="unit-test")
        finally:
            mvv._CUDA_VALIDATION_DISABLED = False

        self.assertEqual(result, ["ok"])
        self.assertEqual(gpu_model.calls[0]["device"], "0")
        self.assertTrue(gpu_model.calls[0]["half"])
        self.assertEqual(cpu_model.calls[0]["device"], "cpu")
        self.assertFalse(cpu_model.calls[0]["half"])

    def test_assembly_priority_validates_top_candidate_modules_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            library = Path(tmp) / "library"
            for suffix, confidence in (("low", 0.4), ("high", 0.95)):
                module_record(library, product="serum", role="hook", module_id=f"serum_hook_{suffix}", confidence=confidence)
                module_record(library, product="serum", role="main", module_id=f"serum_main_{suffix}", confidence=confidence)
                module_record(library, product="serum", role="cta", module_id=f"serum_cta_{suffix}", confidence=confidence)

            class Cfg(VisualCfg):
                MODULE_LIBRARY_DIR = str(library)

            seen = []

            def fake_scan(module_path, product, cfg, model=None):
                seen.append(Path(module_path).stem)
                return {"hits": 1, "confidence_max": 0.8, "events": []}

            with mock.patch.object(mvv, "scan_module_visual_events", side_effect=fake_scan):
                result = mvv.validate_module_library_visual(
                    Cfg,
                    visual_status="all",
                    priority="assembly_ready",
                    limit=3,
                    force=True,
                )

        self.assertEqual(result["validated"], 3)
        self.assertEqual(set(seen), {"serum_hook_high", "serum_main_high", "serum_cta_high"})

    def test_manual_approval_restores_eligibility_but_preserves_visual_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            library = Path(tmp) / "library"
            media, record = module_record(library, quality_status="needs_review")
            record["quality_reason"] = "visual_product_mismatch"
            record["visual_validation_status"] = "failed"
            record["visual_product_hits"] = 0
            record["visual_validation_reason"] = "no_matching_product_detection"
            media.with_suffix(".json").write_text(json.dumps(record), encoding="utf-8")

            class Cfg(VisualCfg):
                MODULE_LIBRARY_DIR = str(library)

            result = update_module_review(record["module_id"], "approved", Cfg, reviewer="qa")
            sidecar = json.loads(media.with_suffix(".json").read_text(encoding="utf-8"))

        self.assertEqual(result["quality_status"], "approved")
        self.assertEqual(sidecar["review_status"], "approved")
        self.assertEqual(sidecar["visual_validation_status"], "failed")


if __name__ == "__main__":
    unittest.main()
