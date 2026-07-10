import random
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from product_broll import (
    build_broll_plan,
    canonical_product,
    product_broll_asset_fingerprint,
    product_broll_preview_sources,
    resolve_moment_product_key,
    resolve_product_events_key,
)


class ProductBrollTests(unittest.TestCase):
    def _cfg(self, root: Path):
        return SimpleNamespace(
            PRODUCT_BROLL_DIR=str(root / "product_broll"),
            PRODUCT_BROLL_CROSSFADE_SECONDS=0.3,
            PRODUCT_BROLL_VIDEO_EXTS={".mp4", ".mov"},
        )

    def _probe(self, durations: dict[str, float]):
        def probe(path: str):
            name = Path(path).name
            duration = durations.get(name)
            if duration is None:
                return None
            return {"duration": duration, "width": 720, "height": 1280, "fps": 30.0}

        return probe

    def test_product_resolution_uses_canonical_aliases_and_moment_text(self):
        self.assertEqual(canonical_product("Eye Cream"), "eye_cream")
        self.assertEqual(canonical_product("skin cream malam"), "skin_cream")
        self.assertEqual(
            resolve_moment_product_key({"product": "general", "selected_text": "pakai toner ini"}),
            "toner",
        )

    def test_product_resolution_uses_detected_events_when_text_is_generic(self):
        events = [
            {"class_name": "skin cream", "duration": 0.1, "best_confidence": 0.69, "detection_count": 1},
            {"class_name": "Toner", "duration": 14.4, "best_confidence": 0.88, "detection_count": 19},
        ]

        self.assertEqual(resolve_product_events_key(events), "toner")
        self.assertEqual(
            resolve_moment_product_key(
                {"product": "PROYA 5X Vitamin C", "selected_text": "harga turun hari ini"},
                product_events=events,
            ),
            "toner",
        )

    def test_broll_plan_can_use_detected_event_product_fallback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cfg = self._cfg(root)
            toner_dir = Path(cfg.PRODUCT_BROLL_DIR) / "toner"
            toner_dir.mkdir(parents=True)
            (toner_dir / "toner.mp4").write_bytes(b"toner")

            plan, reason = build_broll_plan(
                {"product": "PROYA 5X Vitamin C", "selected_text": "harga turun hari ini"},
                cfg,
                1.0,
                rng=random.Random(7),
                probe_fn=self._probe({"toner.mp4": 1.2}),
                product_events=[
                    {"class_name": "Toner", "duration": 14.4, "best_confidence": 0.88, "detection_count": 19},
                ],
            )

            self.assertEqual(reason, "")
            self.assertIsNotNone(plan)
            self.assertEqual(plan.product_key, "toner")

    def test_missing_or_empty_folder_returns_fallback_reason(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cfg = self._cfg(root)

            plan, reason = build_broll_plan({"product": "Serum"}, cfg, 5.0, probe_fn=self._probe({}))
            self.assertIsNone(plan)
            self.assertIn("folder missing", reason)

            serum_dir = Path(cfg.PRODUCT_BROLL_DIR) / "serum"
            serum_dir.mkdir(parents=True)
            plan, reason = build_broll_plan({"product": "Serum"}, cfg, 5.0, probe_fn=self._probe({}))
            self.assertIsNone(plan)
            self.assertIn("no supported videos", reason)

    def test_invalid_video_files_are_ignored(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cfg = self._cfg(root)
            serum_dir = Path(cfg.PRODUCT_BROLL_DIR) / "serum"
            serum_dir.mkdir(parents=True)
            (serum_dir / "bad.mp4").write_bytes(b"not a video")

            plan, reason = build_broll_plan({"product": "Serum"}, cfg, 5.0, probe_fn=self._probe({}))

            self.assertIsNone(plan)
            self.assertIn("no valid video files", reason)

    def test_random_sequence_covers_duration_and_uses_crossfades(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cfg = self._cfg(root)
            serum_dir = Path(cfg.PRODUCT_BROLL_DIR) / "serum"
            serum_dir.mkdir(parents=True)
            (serum_dir / "a.mp4").write_bytes(b"a")
            (serum_dir / "b.mp4").write_bytes(b"b")

            plan, reason = build_broll_plan(
                {"product": "Serum"},
                cfg,
                2.2,
                rng=random.Random(7),
                probe_fn=self._probe({"a.mp4": 1.0, "b.mp4": 0.9}),
            )

            self.assertEqual(reason, "")
            self.assertIsNotNone(plan)
            timeline = sum(clip.duration for clip in plan.clips) - sum(item.duration for item in plan.transitions)
            self.assertGreaterEqual(timeline + 1e-6, 2.2)
            self.assertGreaterEqual(len(plan.clips), 3)
            self.assertTrue(all(item.duration <= 0.3 for item in plan.transitions))

    def test_single_file_can_repeat_to_cover_duration(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cfg = self._cfg(root)
            mask_dir = Path(cfg.PRODUCT_BROLL_DIR) / "mask"
            mask_dir.mkdir(parents=True)
            (mask_dir / "only.mp4").write_bytes(b"x")

            plan, reason = build_broll_plan(
                {"product": "Mask"},
                cfg,
                2.4,
                rng=random.Random(3),
                probe_fn=self._probe({"only.mp4": 1.0}),
            )

            self.assertEqual(reason, "")
            self.assertIsNotNone(plan)
            self.assertGreaterEqual(len(plan.clips), 3)
            self.assertEqual({Path(clip.path).name for clip in plan.clips}, {"only.mp4"})

    def test_preview_metadata_and_asset_fingerprint_track_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cfg = self._cfg(root)
            serum_dir = Path(cfg.PRODUCT_BROLL_DIR) / "serum"
            serum_dir.mkdir(parents=True)
            before = product_broll_asset_fingerprint(cfg)
            (serum_dir / "demo.mp4").write_bytes(b"demo")
            after = product_broll_asset_fingerprint(cfg)
            preview = product_broll_preview_sources(cfg)

            self.assertNotEqual(before, after)
            serum = next(item for item in preview["products"] if item["product_key"] == "serum")
            self.assertTrue(serum["exists"])
            self.assertEqual(serum["video_count"], 1)
            self.assertTrue(serum["preview"]["url"].startswith("/api/artifacts?path="))


if __name__ == "__main__":
    unittest.main()
