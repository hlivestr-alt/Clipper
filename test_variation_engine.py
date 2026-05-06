import unittest
from types import SimpleNamespace

from variation_engine import generate_variants


class VariationGeneratorTests(unittest.TestCase):
    def test_seeded_six_pack_uses_distinct_visible_styles(self):
        base_cfg = SimpleNamespace(
            HOOK_DURATION=1.5,
            ZOOM_SCALE=1.45,
            SUBTITLE_Y_POS=0.80,
            FONT_SUBTITLE="assets/fonts/Montserrat-ExtraBold.ttf",
            KARAOKE_ACTIVE_COLOR="#FFD600",
            KARAOKE_INACTIVE_OPACITY=1.0,
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


if __name__ == "__main__":
    unittest.main()
