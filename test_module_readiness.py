import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from module_readiness import build_product_readiness_from_index, build_visual_readiness_from_index


class ModuleReadinessTests(unittest.TestCase):
    def test_product_readiness_aggregates_per_product_not_per_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            library = Path(tmp)
            modules = []
            for date in ("2026-04-23", "2026-04-24"):
                modules.extend(
                    [{"product": "serum", "role": "hook", "source_video_date": date} for _ in range(3)]
                )
                modules.extend(
                    [{"product": "serum", "role": "main", "source_video_date": date} for _ in range(2)]
                )
                modules.extend(
                    [{"product": "serum", "role": "cta", "source_video_date": date} for _ in range(2)]
                )
            (library / "index.json").write_text(
                json.dumps({"module_count": len(modules), "modules": modules}),
                encoding="utf-8",
            )

            result = build_product_readiness_from_index(library, min_hook=5, min_main=3, min_cta=3)

        serum = next(row for row in result["rows"] if row["product_key"] == "serum")
        self.assertEqual(serum["Hook"], 6)
        self.assertEqual(serum["Main"], 4)
        self.assertEqual(serum["CTA"], 4)
        self.assertEqual(serum["Readiness"], "ready")
        self.assertNotIn("Source Date", serum)

    def test_product_readiness_reads_only_index_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            library = Path(tmp)
            index = library / "index.json"
            sidecar = library / "serum" / "hook" / "serum_hook.json"
            sidecar.parent.mkdir(parents=True)
            sidecar.write_text("{}", encoding="utf-8")
            index.write_text(
                json.dumps(
                    {
                        "modules": [
                            {
                                "product": "serum",
                                "role": "hook",
                                "sidecar_path": str(sidecar),
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            original = Path.read_text

            def guarded_read_text(path, *args, **kwargs):
                if Path(path).name != "index.json":
                    raise AssertionError(f"unexpected sidecar read: {path}")
                return original(path, *args, **kwargs)

            with mock.patch.object(Path, "read_text", guarded_read_text):
                result = build_product_readiness_from_index(library, min_hook=5, min_main=3, min_cta=3)

        serum = next(row for row in result["rows"] if row["product_key"] == "serum")
        self.assertEqual(serum["Hook"], 1)
        self.assertEqual(serum["Readiness"], "partial")

    def test_visual_readiness_reads_only_index_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            library = Path(tmp)
            index = library / "index.json"
            sidecar = library / "serum" / "hook" / "serum_hook.json"
            sidecar.parent.mkdir(parents=True)
            sidecar.write_text("{}", encoding="utf-8")
            index.write_text(
                json.dumps(
                    {
                        "modules": [
                            {
                                "product": "serum",
                                "role": "hook",
                                "quality_status": "approved",
                                "visual_validation_status": "passed",
                                "visual_product_hits": 1,
                                "sidecar_path": str(sidecar),
                            },
                            {
                                "product": "serum",
                                "role": "main",
                                "quality_status": "approved",
                                "visual_validation_status": "not_run",
                                "visual_product_hits": 0,
                                "sidecar_path": str(sidecar),
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            original = Path.read_text

            def guarded_read_text(path, *args, **kwargs):
                if Path(path).name != "index.json":
                    raise AssertionError(f"unexpected sidecar read: {path}")
                return original(path, *args, **kwargs)

            with mock.patch.object(Path, "read_text", guarded_read_text):
                result = build_visual_readiness_from_index(library, min_events=1)

        serum = next(row for row in result["rows"] if row["product_key"] == "serum")
        self.assertEqual(serum["Passed"], 1)
        self.assertEqual(serum["Not Run"], 1)
        self.assertEqual(serum["Visual Coverage %"], 50.0)
        self.assertEqual(serum["Zoom-ready Candidates"], 1)
        self.assertTrue(serum["Zoom Ready"])


if __name__ == "__main__":
    unittest.main()
