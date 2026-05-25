import json
import tempfile
import unittest
from pathlib import Path

import module_report


class ReportTests(unittest.TestCase):
    def test_library_report_writes_health_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            library = root / "library"
            working = root / "working" / "vod"
            module_dir = library / "serum" / "hook"
            module_dir.mkdir(parents=True)
            media = module_dir / "serum_hook_20260509_0.mp4"
            media.write_bytes(b"video")
            media.with_suffix(".json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "module_id": media.stem,
                        "product": "serum",
                        "role": "hook",
                        "source_video": "vod.mp4",
                        "file_path": str(media),
                        "start": 0.0,
                        "end": 6.0,
                        "duration": 6.0,
                        "confidence": 0.9,
                        "quality_status": "approved",
                        "boundary_mode": "sentence",
                        "visual_validation_status": "passed",
                        "visual_product_hits": 1,
                    }
                ),
                encoding="utf-8",
            )
            working.mkdir(parents=True)
            (working / "module_candidates.json").write_text(
                json.dumps({"candidates": [{"extraction_status": "rejected", "rejection_reason": "unknown_product"}]}),
                encoding="utf-8",
            )

            class Cfg:
                MODULE_LIBRARY_DIR = str(library)
                WORKING_DIR = str(root / "working")
                MODULE_INDEX_VALIDATE_MEDIA = False
                MODULE_WORD_FALLBACK_REVIEW_REQUIRED = True
                MODULE_ASSEMBLY_REQUIRE_APPROVED = True
                MODULAR_ASSEMBLY_READY_MIN_HOOK = 1
                MODULAR_ASSEMBLY_READY_MIN_MAIN = 1
                MODULAR_ASSEMBLY_READY_MIN_CTA = 1
                MODULE_ASSEMBLY_MIN_SOURCE_VIDEOS = 1
                MODULE_INDEX_LOCK_TIMEOUT = 0.1

            report = module_report.build_module_library_report(Cfg)

            self.assertTrue(Path(report["json_path"]).exists())
            self.assertTrue(Path(report["csv_path"]).exists())
            self.assertEqual(report["module_count"], 1)
            self.assertEqual(report["rejection_reasons"][0]["reason"], "unknown_product")
            self.assertEqual(report["sidecar_load_error_count"], 0)
            usable_rows = report["counts_by_product_role_usable"]
            visual_rows = report["counts_by_product_role_visual"]
            self.assertIn(
                {"product": "serum", "role": "hook", "usable_for_assembly": "false", "count": 1},
                usable_rows,
            )
            self.assertIn(
                {"product": "serum", "role": "hook", "visual_validation_status": "passed", "count": 1},
                visual_rows,
            )
            visual_ready = next(
                row
                for row in report["visual_readiness"]
                if row["product"] == "serum" and row["role"] == "hook"
            )
            self.assertEqual(visual_ready["total_modules"], 1)
            self.assertEqual(visual_ready["approved_modules"], 1)
            self.assertEqual(visual_ready["passed"], 1)
            self.assertEqual(visual_ready["visual_coverage_percent"], 100.0)
            self.assertEqual(visual_ready["zoom_ready_candidate_count"], 1)
            serum = next(row for row in report["readiness"] if row["product"] == "serum")
            self.assertFalse(serum["ready"])
            self.assertEqual(serum["usable_hook"], 0)

    def test_report_readiness_uses_role_specific_thresholds(self):
        modules = [
            {
                "product": "serum",
                "role": "hook",
                "quality_status": "approved",
                "usable_for_assembly": True,
                "source_video": f"vod_{index}.mp4",
            }
            for index in range(4)
        ]
        modules.extend(
            {
                "product": "serum",
                "role": "main",
                "quality_status": "approved",
                "usable_for_assembly": True,
                "source_video": f"vod_{index}.mp4",
            }
            for index in range(3)
        )
        modules.extend(
            {
                "product": "serum",
                "role": "cta",
                "quality_status": "approved",
                "usable_for_assembly": True,
                "source_video": f"vod_{index}.mp4",
            }
            for index in range(3)
        )

        class Cfg:
            MODULAR_ASSEMBLY_READY_MIN_HOOK = 5
            MODULAR_ASSEMBLY_READY_MIN_MAIN = 3
            MODULAR_ASSEMBLY_READY_MIN_CTA = 3
            MODULE_ASSEMBLY_MIN_SOURCE_VIDEOS = 1

        rows = module_report._readiness_by_product(modules, Cfg)
        serum = next(row for row in rows if row["product"] == "serum")

        self.assertFalse(serum["ready"])
        self.assertEqual(serum["reason"], "hook<5")

    def test_no_visual_events_modules_count_as_readiness_eligible(self):
        modules = [
            {
                "product": "serum",
                "role": "hook",
                "quality_status": "no_visual_events",
                "usable_for_assembly": True,
                "source_video": "vod_hook.mp4",
            },
            {
                "product": "serum",
                "role": "main",
                "quality_status": "no_visual_events",
                "usable_for_assembly": True,
                "source_video": "vod_main.mp4",
            },
            {
                "product": "serum",
                "role": "cta",
                "quality_status": "no_visual_events",
                "usable_for_assembly": True,
                "source_video": "vod_cta.mp4",
            },
        ]

        class Cfg:
            MODULAR_ASSEMBLY_READY_MIN_HOOK = 1
            MODULAR_ASSEMBLY_READY_MIN_MAIN = 1
            MODULAR_ASSEMBLY_READY_MIN_CTA = 1
            MODULE_ASSEMBLY_MIN_SOURCE_VIDEOS = 1

        rows = module_report._readiness_by_product(modules, Cfg)
        serum = next(row for row in rows if row["product"] == "serum")

        self.assertTrue(serum["ready"])
        self.assertEqual(serum["usable_hook"], 1)
        self.assertEqual(serum["usable_main"], 1)
        self.assertEqual(serum["usable_cta"], 1)


if __name__ == "__main__":
    unittest.main()
