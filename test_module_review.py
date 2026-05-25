import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import main
import module_review


class ReviewCfg:
    MODULE_INDEX_VALIDATE_MEDIA = False
    MODULE_INDEX_LOCK_TIMEOUT = 0.1
    MODULE_FILE_LOCK_TIMEOUT = 0.1
    MODULE_WORD_FALLBACK_REVIEW_REQUIRED = True
    MODULE_PRODUCT_EVIDENCE_REQUIRED = False


def write_module(root, product, role, module_id, quality_status, boundary_mode="sentence"):
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
        "end": 6.0,
        "duration": 6.0,
        "confidence": 0.9,
        "quality_status": quality_status,
        "quality_reason": f"{quality_status}_reason",
        "review_status": "pending",
        "boundary_mode": boundary_mode,
        "transcript_text": f"{product} sample text.",
        "words": [{"word": product, "start": 0.0, "end": 0.4}],
    }
    media.with_suffix(".json").write_text(json.dumps(record), encoding="utf-8")
    return media, record


class ModuleReviewTests(unittest.TestCase):
    def test_review_queue_writes_filtered_needs_review_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            library = Path(tmp) / "library"

            class Cfg(ReviewCfg):
                MODULE_LIBRARY_DIR = str(library)

            write_module(library, "serum", "hook", "serum_hook_review", "needs_review")
            write_module(library, "serum", "main", "serum_main_approved", "approved")

            result = module_review.build_module_review_queue(Cfg)

            self.assertEqual(result["module_count"], 1)
            self.assertEqual(result["modules"][0]["module_id"], "serum_hook_review")
            self.assertTrue(Path(result["json_path"]).exists())
            self.assertTrue(Path(result["csv_path"]).exists())

    def test_review_queue_filters_product_role_and_all_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            library = Path(tmp) / "library"

            class Cfg(ReviewCfg):
                MODULE_LIBRARY_DIR = str(library)

            write_module(library, "serum", "hook", "serum_hook_review", "needs_review")
            write_module(library, "serum", "main", "serum_main_approved", "approved")
            write_module(library, "toner", "hook", "toner_hook_review", "needs_review")

            result = module_review.build_module_review_queue(
                Cfg,
                status="all",
                product="serum",
                role="hook",
            )

            self.assertEqual(result["module_count"], 1)
            self.assertEqual(result["modules"][0]["module_id"], "serum_hook_review")

    def test_review_update_approves_sidecar_and_rebuilds_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            library = Path(tmp) / "library"

            class Cfg(ReviewCfg):
                MODULE_LIBRARY_DIR = str(library)

            media, _record = write_module(
                library,
                "serum",
                "hook",
                "serum_hook_review",
                "needs_review",
                boundary_mode="word_boundary_fallback",
            )

            result = module_review.update_module_review(
                "serum_hook_review",
                "approved",
                Cfg,
                note="good sentence despite fallback",
                reviewer="qa",
            )
            sidecar = json.loads(media.with_suffix(".json").read_text(encoding="utf-8"))
            index = json.loads((library / "index.json").read_text(encoding="utf-8"))

            self.assertEqual(result["quality_status"], "approved")
            self.assertEqual(sidecar["review_status"], "approved")
            self.assertEqual(sidecar["quality_status"], "approved")
            self.assertEqual(sidecar["reviewer"], "qa")
            self.assertEqual(index["module_count"], 1)
            self.assertEqual(index["modules"][0]["quality_status"], "approved")

    def test_review_update_blocks_module_by_media_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            library = Path(tmp) / "library"

            class Cfg(ReviewCfg):
                MODULE_LIBRARY_DIR = str(library)

            media, _record = write_module(library, "toner", "cta", "toner_cta_bad", "approved")

            result = module_review.update_module_review(media, "blocked", Cfg, note="bad claim")
            sidecar = json.loads(media.with_suffix(".json").read_text(encoding="utf-8"))

            self.assertEqual(result["quality_status"], "blocked")
            self.assertEqual(sidecar["review_status"], "blocked")
            self.assertEqual(sidecar["quality_reason"], "manual_review_blocked")
            self.assertEqual(sidecar["quality_score"], 0.0)

    def test_cli_review_queue_does_not_require_video(self):
        argv = [
            "main.py",
            "--module-review-queue",
            "--module-review-filter",
            "blocked",
            "--module-review-limit",
            "5",
        ]
        with mock.patch.object(sys, "argv", argv), \
            mock.patch("module_review.build_module_review_queue", return_value={"module_count": 0, "counts_by_quality_status": []}) as build_queue:
            main.main()

        build_queue.assert_called_once()
        self.assertEqual(build_queue.call_args.kwargs["status"], "blocked")
        self.assertEqual(build_queue.call_args.kwargs["limit"], 5)

    def test_cli_review_set_requires_status(self):
        argv = ["main.py", "--module-review-set", "serum_hook_review"]
        with mock.patch.object(sys, "argv", argv), self.assertRaises(SystemExit):
            main.main()


if __name__ == "__main__":
    unittest.main()
