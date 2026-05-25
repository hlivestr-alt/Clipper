import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from variation_engine import (
    _apply_variant_timeline_offsets,
    apply_variant_to_cfg,
    expand_moments_with_variants,
    generate_variants,
)


class VariationGeneratorTests(unittest.TestCase):
    def test_variant_cfg_preserves_base_settings_from_runtime_wrapper(self):
        base_cfg = SimpleNamespace(
            HOOK_DURATION=1.5,
            ZOOM_SCALE=1.45,
            SUBTITLE_Y_POS=0.80,
            FONT_SUBTITLE="assets/fonts/Montserrat-ExtraBold.ttf",
            FONT_HOOK="assets/fonts/Poppins-Bold.ttf",
            BEFORE_AFTER_ENABLED=True,
            BEFORE_AFTER_DIR="assets/before_after",
            KARAOKE_ACTIVE_COLOR="#FFD600",
            KARAOKE_INACTIVE_OPACITY=1.0,
            BROLL_INTRO_ENABLED=False,
        )

        class RuntimeCfg:
            def __init__(self, base):
                self._base = base

            def __getattr__(self, name):
                return getattr(self._base, name)

        runtime_cfg = RuntimeCfg(base_cfg)
        variant = generate_variants(runtime_cfg, 6, seed=42)[1]
        patched = apply_variant_to_cfg(runtime_cfg, variant)

        self.assertTrue(patched.BEFORE_AFTER_ENABLED)
        self.assertEqual(patched.BEFORE_AFTER_DIR, "assets/before_after")
        self.assertEqual(patched.FONT_HOOK, "assets/fonts/Poppins-Bold.ttf")
        self.assertEqual(patched.HOOK_COLOR, variant.hook_color)
        self.assertEqual(patched._variant_archetype, variant.archetype)
        self.assertEqual(patched._hook_layout_mode, variant.hook_layout_mode)
        self.assertEqual(patched._before_after_variant_mode, variant.before_after_variant_mode)

    def test_seeded_six_pack_uses_distinct_visible_styles(self):
        base_cfg = SimpleNamespace(
            HOOK_DURATION=1.5,
            ZOOM_SCALE=1.45,
            SUBTITLE_Y_POS=0.80,
            FONT_SUBTITLE="assets/fonts/Montserrat-ExtraBold.ttf",
            KARAOKE_ACTIVE_COLOR="#FFD600",
            KARAOKE_INACTIVE_OPACITY=1.0,
            BROLL_INTRO_ENABLED=False,
        )

        variants = generate_variants(base_cfg, 6, seed=42)
        repeat_variants = generate_variants(base_cfg, 6, seed=42)

        self.assertEqual(variants, repeat_variants)
        self.assertEqual(variants[0].variant_id, "v0_original")

        mutated = variants[1:]
        self.assertEqual(len(mutated), 5)
        self.assertEqual(len({variant.variant_id for variant in variants}), 6)
        self.assertEqual(
            len({variant.variant_id.split("_", 1)[1] for variant in mutated}),
            len(mutated),
        )

        subtitle_styles = {
            (
                variant.font_subtitle,
                variant.karaoke_active_color,
                variant.karaoke_inactive_opacity,
                variant.subtitle_stroke,
                variant.subtitle_stroke_w,
            )
            for variant in mutated
        }
        hook_styles = {
            (
                variant.hook_color,
                variant.hook_stroke_color,
                variant.hook_stroke_w,
                variant.hook_fontsize_mult,
            )
            for variant in mutated
        }

        self.assertEqual(len(subtitle_styles), len(mutated))
        self.assertEqual(len(hook_styles), len(mutated))

    def test_seeded_six_pack_uses_named_archetype_slots(self):
        base_cfg = SimpleNamespace(
            HOOK_DURATION=1.5,
            ZOOM_SCALE=1.45,
            SUBTITLE_Y_POS=0.80,
            FONT_SUBTITLE="assets/fonts/Montserrat-ExtraBold.ttf",
            KARAOKE_ACTIVE_COLOR="#FFD600",
            KARAOKE_INACTIVE_OPACITY=1.0,
            BROLL_INTRO_ENABLED=False,
        )

        variants = generate_variants(base_cfg, 6, seed=42)

        self.assertEqual(
            [variant.archetype for variant in variants],
            [
                "original",
                "product_broll_open",
                "tight_product_focus",
                "result_overlay",
                "host_focus_fast",
                "clean_commerce",
            ],
        )
        self.assertEqual(
            [variant.variant_id for variant in variants],
            [
                "v0_original",
                "v1_product_broll_open",
                "v2_tight_product_focus",
                "v3_result_overlay",
                "v4_host_focus_fast",
                "v5_clean_commerce",
            ],
        )
        self.assertGreaterEqual(
            len({variant.hook_layout_mode for variant in variants[1:]}),
            4,
        )
        self.assertGreaterEqual(
            len({variant.before_after_variant_mode for variant in variants[1:]}),
            4,
        )

    def test_timeline_offsets_clamp_near_zero_and_keep_valid_duration(self):
        base_cfg = SimpleNamespace(
            HOOK_DURATION=1.5,
            ZOOM_SCALE=1.45,
            SUBTITLE_Y_POS=0.80,
            FONT_SUBTITLE="assets/fonts/Montserrat-ExtraBold.ttf",
            KARAOKE_ACTIVE_COLOR="#FFD600",
            KARAOKE_INACTIVE_OPACITY=1.0,
            BROLL_INTRO_ENABLED=False,
        )
        variant = generate_variants(base_cfg, 6, seed=42)[1]
        moment = {"clip_id": "clip_0001", "start": 0.1, "end": 20.1}

        adjusted = _apply_variant_timeline_offsets(moment, variant)

        self.assertEqual(adjusted["start"], 0.0)
        self.assertGreater(adjusted["end"], adjusted["start"])
        self.assertAlmostEqual(adjusted["end"] - adjusted["start"], 20.0, places=3)

    def test_expanded_moments_include_timeline_offsets(self):
        base_cfg = SimpleNamespace(
            HOOK_DURATION=1.5,
            ZOOM_SCALE=1.45,
            SUBTITLE_Y_POS=0.80,
            FONT_SUBTITLE="assets/fonts/Montserrat-ExtraBold.ttf",
            KARAOKE_ACTIVE_COLOR="#FFD600",
            KARAOKE_INACTIVE_OPACITY=1.0,
            BROLL_INTRO_ENABLED=False,
        )
        moments = [{
            "clip_id": "clip_0001",
            "start": 10.0,
            "end": 40.0,
            "score": 9,
            "hook": "Serum best seller",
            "product": "Serum",
            "selected_text": "pakai serum proya ini",
        }]

        expanded = expand_moments_with_variants(moments, base_cfg, n_variants=6, seed=42)
        by_archetype = {moment["_variant"].archetype: moment for moment in expanded}

        self.assertEqual(by_archetype["original"]["start"], 10.0)
        self.assertLess(by_archetype["product_broll_open"]["start"], 10.0)
        self.assertGreater(by_archetype["host_focus_fast"]["start"], 10.0)
        self.assertTrue(all(moment["end"] > moment["start"] for moment in expanded))

    def test_broll_intro_assets_are_assigned_to_some_expanded_variants(self):
        with TemporaryDirectory() as tmp_dir:
            Path(tmp_dir, "intro_a.mp4").touch()
            Path(tmp_dir, "intro_b.mov").touch()
            base_cfg = SimpleNamespace(
                HOOK_DURATION=1.5,
                ZOOM_SCALE=1.45,
                SUBTITLE_Y_POS=0.80,
                FONT_SUBTITLE="assets/fonts/Montserrat-ExtraBold.ttf",
                KARAOKE_ACTIVE_COLOR="#FFD600",
                KARAOKE_INACTIVE_OPACITY=1.0,
                BROLL_INTRO_ENABLED=True,
                BROLL_INTRO_DIR=tmp_dir,
                BROLL_INTRO_MIN_VARIANT_RATE=0.20,
                BROLL_INTRO_MAX_VARIANT_RATE=0.40,
                BROLL_INTRO_APPLY_TO_ORIGINAL=False,
                BROLL_INTRO_MAX_DURATION=2.5,
                BROLL_INTRO_REQUIRE_PRODUCT_MATCH=False,
            )
            moments = [{
                "clip_id": "clip_0001",
                "start": 0,
                "end": 30,
                "score": 9,
                "hook": "Serum best seller",
                "product": "Serum",
                "selected_text": "pakai serum proya ini",
            }]

            expanded = expand_moments_with_variants(moments, base_cfg, n_variants=6, seed=42)
            broll_variants = [
                moment["_variant"]
                for moment in expanded
                if moment["_variant"].broll_intro_path
            ]

            self.assertGreaterEqual(len(broll_variants), 1)
            self.assertLessEqual(len(broll_variants), 2)
            self.assertFalse(expanded[0]["_variant"].broll_intro_path)
            self.assertTrue(all("_broll" in variant.variant_id for variant in broll_variants))
            self.assertTrue(
                all(Path(variant.broll_intro_path).parent == Path(tmp_dir) for variant in broll_variants)
            )

    def test_broll_intro_slot_varies_by_base_clip(self):
        with TemporaryDirectory() as tmp_dir:
            Path(tmp_dir, "intro_a.mp4").touch()
            Path(tmp_dir, "intro_b.mp4").touch()
            base_cfg = SimpleNamespace(
                HOOK_DURATION=1.5,
                ZOOM_SCALE=1.45,
                SUBTITLE_Y_POS=0.80,
                FONT_SUBTITLE="assets/fonts/Montserrat-ExtraBold.ttf",
                KARAOKE_ACTIVE_COLOR="#FFD600",
                KARAOKE_INACTIVE_OPACITY=1.0,
                BROLL_INTRO_ENABLED=True,
                BROLL_INTRO_DIR=tmp_dir,
                BROLL_INTRO_MIN_VARIANT_RATE=0.20,
                BROLL_INTRO_MAX_VARIANT_RATE=0.20,
                BROLL_INTRO_APPLY_TO_ORIGINAL=False,
                BROLL_INTRO_MAX_DURATION=2.5,
                BROLL_INTRO_REQUIRE_PRODUCT_MATCH=False,
            )
            moments = [
                {
                    "clip_id": f"clip_{idx:04d}",
                    "start": idx * 40,
                    "end": idx * 40 + 30,
                    "score": 9,
                    "hook": "Serum best seller",
                    "product": "Serum",
                    "selected_text": "pakai serum proya ini",
                }
                for idx in range(1, 12)
            ]

            expanded = expand_moments_with_variants(moments, base_cfg, n_variants=6, seed=42)
            broll_slots = {
                moment["clip_id"].split("_v", 1)[0]: moment["_variant"].variant_index
                for moment in expanded
                if moment["_variant"].broll_intro_path
            }

            self.assertEqual(len(broll_slots), len(moments))
            self.assertGreater(len(set(broll_slots.values())), 1)
            self.assertNotIn(0, broll_slots.values())

    def test_product_broll_intro_uses_matching_product_folder(self):
        with TemporaryDirectory() as tmp_dir:
            serum_dir = Path(tmp_dir, "Serum")
            toner_dir = Path(tmp_dir, "Toner")
            serum_dir.mkdir()
            toner_dir.mkdir()
            Path(serum_dir, "serum_intro.mp4").touch()
            Path(toner_dir, "toner_intro.mp4").touch()
            base_cfg = SimpleNamespace(
                HOOK_DURATION=1.5,
                ZOOM_SCALE=1.45,
                SUBTITLE_Y_POS=0.80,
                FONT_SUBTITLE="assets/fonts/Montserrat-ExtraBold.ttf",
                KARAOKE_ACTIVE_COLOR="#FFD600",
                KARAOKE_INACTIVE_OPACITY=1.0,
                PRODUCT_CLASSES={3: "Serum", 5: "Toner"},
                BROLL_INTRO_ENABLED=True,
                BROLL_INTRO_DIR=tmp_dir,
                BROLL_INTRO_MIN_VARIANT_RATE=0.40,
                BROLL_INTRO_MAX_VARIANT_RATE=0.40,
                BROLL_INTRO_APPLY_TO_ORIGINAL=False,
                BROLL_INTRO_MAX_DURATION=2.5,
                BROLL_INTRO_REQUIRE_PRODUCT_MATCH=True,
                BROLL_INTRO_ALLOW_GENERIC_ROOT=False,
                BROLL_INTRO_PRODUCT_ALIASES={"Serum": ["serum"], "Toner": ["toner"]},
            )
            moments = [{
                "clip_id": "clip_0001",
                "start": 0,
                "end": 30,
                "score": 9,
                "hook": "Serum best seller",
                "product": "Serum",
                "selected_text": "pakai serum proya ini",
            }]

            expanded = expand_moments_with_variants(moments, base_cfg, n_variants=6, seed=42)
            broll_variants = [
                moment["_variant"]
                for moment in expanded
                if moment["_variant"].broll_intro_path
            ]

            self.assertGreaterEqual(len(broll_variants), 1)
            self.assertTrue(
                all(Path(variant.broll_intro_path).parent == serum_dir for variant in broll_variants)
            )
            self.assertTrue(all(variant.broll_intro_product == "serum" for variant in broll_variants))


if __name__ == "__main__":
    unittest.main()
