import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from variation_engine import expand_moments_with_variants, generate_variants


class VariationGeneratorTests(unittest.TestCase):
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

    def test_broll_intro_assets_are_assigned_to_some_mutated_variants(self):
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

            variants = generate_variants(base_cfg, 6, seed=42)
            broll_variants = [variant for variant in variants if variant.broll_intro_path]

            self.assertGreaterEqual(len(broll_variants), 1)
            self.assertLessEqual(len(broll_variants), 2)
            self.assertFalse(variants[0].broll_intro_path)
            self.assertTrue(all("_broll" in variant.variant_id for variant in broll_variants))
            self.assertTrue(
                all(Path(variant.broll_intro_path).parent == Path(tmp_dir) for variant in broll_variants)
            )

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
