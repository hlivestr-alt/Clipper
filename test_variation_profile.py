import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from variation_profile import (
    VariationRevisionConflict,
    _append_preview_dynamic_filters,
    _preview_subtitle_words,
    active_profile_revision,
    default_profile,
    generate_previews,
    load_active_profile,
    normalize_profile,
    preview_source_ref,
    save_active_profile,
    variation_options,
)


class VariationProfileTests(unittest.TestCase):
    def _cfg(self, root: Path):
        return SimpleNamespace(
            WORKING_DIR=str(root / "working"),
            OUTPUT_DIR=str(root / "output"),
            VARIANTS_PER_CLIP=9,
            FONT_SUBTITLE="assets/fonts/Montserrat-ExtraBold.ttf",
            FONT_HOOK="assets/fonts/Montserrat-ExtraBold.ttf",
            FONT_HOOK_FALLBACKS=[],
            SUBTITLE_FONT_DIR="assets/fonts",
            BGM_DIR=str(root / "bgm"),
        )

    def test_default_profile_clamps_count_and_assigns_new_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = self._cfg(Path(temp_dir))

            profile = default_profile(cfg)

            self.assertEqual(profile["schema_version"], 12)
            self.assertEqual(profile["variant_count"], 6)
            self.assertEqual(
                [idx for idx, item in enumerate(profile["variants"]) if item["letterbox_enabled"]],
                [5],
            )
            self.assertEqual(profile["variants"][0]["hook_type"], "text")
            self.assertEqual(profile["variants"][0]["visual_mode"], "host")
            self.assertFalse(profile["variants"][0]["random_broll_enabled"])
            self.assertTrue(profile["variants"][0]["subtitle_enabled"])
            self.assertEqual(profile["variants"][0]["subtitle_size"], "medium")
            self.assertEqual(profile["variants"][0]["dynamic_text_mode"], "balanced")
            self.assertEqual(
                profile["variants"][0]["dynamic_text_roles"],
                ["ingredients", "benefits", "usage", "cta"],
            )
            self.assertEqual(profile["variants"][0]["dynamic_text_settings"]["ingredients"]["font_size"], 35)
            self.assertEqual(profile["variants"][0]["dynamic_text_settings"]["ingredients"]["duration_seconds"], 2.6)
            self.assertEqual(profile["variants"][0]["dynamic_text_settings"]["cta"]["font_size"], 50)
            self.assertEqual(profile["variants"][0]["dynamic_text_settings"]["cta"]["duration_seconds"], 1.3)
            self.assertEqual(profile["variants"][0]["text_style_id"], "current")
            self.assertEqual(profile["variants"][0]["headline_font_id"], profile["variants"][0]["font_id"])
            self.assertEqual(profile["variants"][0]["caption_font_id"], profile["variants"][0]["font_id"])
            self.assertTrue(profile["variants"][0]["product_zoom_enabled"])
            self.assertFalse(profile["variants"][0]["mirror_enabled"])
            self.assertEqual(profile["variants"][0]["before_after_mode"], "fullscreen")
            self.assertEqual(profile["variants"][0]["subtitle_y_frac"], 0.84)
            self.assertEqual(profile["variants"][0]["letterbox_top_frac"], 0.0)
            self.assertEqual(profile["variants"][0]["letterbox_bottom_frac"], 0.0)
            self.assertFalse(profile["variants"][0]["letterbox_hook_enabled"])
            self.assertEqual(profile["variants"][0]["letterbox_hook_font_id"], profile["variants"][0]["font_id"])
            self.assertEqual(profile["variants"][0]["letterbox_hook_font_color"], "#FFFFFF")
            self.assertEqual(profile["variants"][0]["letterbox_hook_font_size"], 72)
            self.assertEqual(profile["variants"][0]["letterbox_hook_x_frac"], 0.5)
            self.assertEqual(profile["variants"][0]["letterbox_hook_y_frac"], 0.5)
            self.assertEqual(profile["variants"][5]["letterbox_top_frac"], 0.20)
            self.assertEqual(profile["variants"][5]["letterbox_bottom_frac"], 0.20)

    def test_preview_subtitle_demo_contains_three_stable_timed_cues(self):
        words = _preview_subtitle_words()
        grouped = {}
        for word in words:
            grouped.setdefault(word["_subtitle_group"], []).append(word["word"])

        self.assertEqual(
            [" ".join(grouped[index]) for index in sorted(grouped)],
            [
                "Ini contoh gaya subtitle",
                "Posisi dan ukuran mengikuti varian",
                "Animasi terlihat di hasil render",
            ],
        )
        self.assertGreaterEqual(words[0]["start"], 0.0)
        self.assertLessEqual(words[-1]["end"], 6.0)

    def test_preview_dynamic_filters_are_box_free_and_side_aligned(self):
        filters = []
        variant = {
            "dynamic_text_mode": "balanced",
            "subtitle_position": "bottom",
            "letterbox_top_frac": 0.0,
            "letterbox_bottom_frac": 0.0,
        }

        _append_preview_dynamic_filters(
            filters,
            variant,
            1,
            {"lines": ["Niacinamide", "Menjaga kelembapan", "Gunakan setelah cleanser"]},
            headline_font="",
            caption_font="",
            check_font="",
            font_color="white",
            highlight="yellow",
        )

        graph = ",".join(filters)
        self.assertNotIn("drawbox", graph)
        self.assertIn("FUNGSI PRODUK", graph)
        self.assertIn("WORTH DICOBA?", graph)
        self.assertRegex(graph, r"x='\d+[+-]16\*")
        self.assertIn("fontsize=15", graph)
        self.assertIn("fontsize=11", graph)
        self.assertIn("between(t,2.35,4.95)", graph)
        self.assertNotIn("#32D583", graph)

        disabled_filters = []
        _append_preview_dynamic_filters(
            disabled_filters,
            {**variant, "dynamic_text_mode": "off"},
            1,
            {},
            headline_font="",
            caption_font="",
            check_font="",
            font_color="white",
            highlight="yellow",
        )
        self.assertEqual(disabled_filters, [])

    def test_preview_rotates_one_information_section_per_variant(self):
        preview_content = {
            "role_lines": {
                "ingredients": ["Niacinamide"],
                "benefits": ["Menjaga kelembapan"],
                "usage": ["Gunakan setelah cleanser"],
            },
        }
        rendered = []
        for index in range(3):
            filters = []
            _append_preview_dynamic_filters(
                filters,
                {
                    "dynamic_text_mode": "balanced",
                    "dynamic_text_roles": ["ingredients", "benefits", "usage"],
                    "subtitle_position": "bottom",
                },
                index,
                preview_content,
                headline_font="",
                caption_font="",
                check_font="",
                font_color="white",
                highlight="yellow",
            )
            rendered.append(",".join(filters))

        self.assertIn("KANDUNGAN UTAMA", rendered[0])
        self.assertNotIn("FUNGSI PRODUK", rendered[0])
        self.assertIn("FUNGSI PRODUK", rendered[1])
        self.assertNotIn("CARA PAKAI", rendered[1])
        self.assertIn("CARA PAKAI", rendered[2])
        self.assertNotIn("KANDUNGAN UTAMA", rendered[2])
        self.assertIn("fontsize=10", rendered[2])

    def test_variation_options_report_global_feature_prerequisites(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = self._cfg(Path(temp_dir))
            cfg.SFX_ENABLED = False
            cfg.BGM_ENABLED = False
            cfg.BEFORE_AFTER_ENABLED = False
            cfg.BROLL_INTRO_ENABLED = False
            cfg.TRANSITIONAL_HOOK_ENABLED = False
            cfg.HOST_FACE_ZOOM_ENABLED = False
            self.assertEqual(
                variation_options(cfg)["global_feature_flags"],
                {
                    "sfx": False,
                    "bgm": False,
                    "before_after": False,
                    "broll_intro": False,
                    "transitional_hook": False,
                    "host_face_zoom": False,
                },
            )
    def test_schema_v2_payload_migrates_and_clamps_preview_layout_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = self._cfg(Path(temp_dir))

            loaded = normalize_profile(
                {
                    "schema_version": 2,
                    "variant_count": 3,
                    "variants": [
                        {"name": "Top", "subtitle_position": "top", "letterbox_enabled": True},
                        {
                            "name": "Center",
                            "subtitle_position": "center",
                            "subtitle_y_frac": 2.0,
                            "letterbox_enabled": True,
                            "letterbox_top_frac": -1,
                            "letterbox_bottom_frac": 1,
                        },
                        {
                            "name": "Bottom",
                            "subtitle_position": "bottom",
                            "subtitle_y_frac": 0.01,
                            "letterbox_enabled": False,
                        },
                    ],
                },
                cfg,
            )

            self.assertEqual(loaded["schema_version"], 12)
            self.assertEqual(loaded["variants"][0]["visual_mode"], "host")
            self.assertEqual(loaded["variants"][0]["subtitle_size"], "medium")
            self.assertFalse(loaded["variants"][0]["letterbox_hook_enabled"])
            self.assertEqual(loaded["variants"][0]["letterbox_hook_font_size"], 72)
            self.assertFalse(loaded["variants"][0]["mirror_enabled"])
            self.assertEqual(loaded["variants"][0]["before_after_mode"], "fullscreen")
            self.assertEqual(loaded["variants"][0]["subtitle_y_frac"], 0.34)
            self.assertEqual(loaded["variants"][0]["letterbox_top_frac"], 0.20)
            self.assertEqual(loaded["variants"][0]["letterbox_bottom_frac"], 0.20)
            self.assertEqual(loaded["variants"][1]["subtitle_y_frac"], 0.92)
            self.assertEqual(loaded["variants"][1]["letterbox_top_frac"], 0.0)
            self.assertEqual(loaded["variants"][1]["letterbox_bottom_frac"], 0.40)
            self.assertEqual(loaded["variants"][2]["subtitle_y_frac"], 0.08)
            self.assertEqual(loaded["variants"][2]["letterbox_top_frac"], 0.0)
            self.assertEqual(loaded["variants"][2]["letterbox_bottom_frac"], 0.0)

    def test_creator_bold_pop_style_supplies_reference_defaults(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = self._cfg(Path(temp_dir))

            loaded = normalize_profile(
                {
                    "variant_count": 1,
                    "variants": [
                        {
                            "name": "Reference",
                            "text_style_id": "creator_bold_pop",
                        }
                    ],
                },
                cfg,
            )

            variant = loaded["variants"][0]
            self.assertEqual(variant["font_id"], "assets/fonts/Montserrat-ExtraBold.ttf")
            self.assertEqual(variant["headline_font_id"], "assets/fonts/Montserrat-ExtraBold.ttf")
            self.assertEqual(variant["caption_font_id"], "assets/fonts/Montserrat-ExtraBold.ttf")
            self.assertEqual(variant["subtitle_size"], "compact")
            self.assertEqual(variant["subtitle_y_frac"], 0.67)
            self.assertEqual(variant["subtitle_stroke_width"], 5)
            self.assertFalse(variant["subtitle_highlight_enabled"])
            self.assertEqual(variant["headline_animation"], "pop_overshoot")
            self.assertEqual(variant["caption_animation"], "staggered_reveal")
            self.assertEqual(variant["headline_shadow_color"], "#FF719B")
            self.assertEqual(variant["headline_rotation_degrees"], -2.0)

            styles = variation_options(cfg)["text_styles"]
            self.assertEqual(styles[0]["id"], "current")
            self.assertTrue(any(item["id"] == "creator_bold_pop" for item in styles))

    def test_transitional_hook_is_available_and_normalizes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = self._cfg(Path(temp_dir))

            loaded = normalize_profile(
                {
                    "variant_count": 1,
                    "variants": [{"name": "Viral opener", "hook_type": "transitional_hook"}],
                },
                cfg,
            )

            self.assertIn("transitional_hook", variation_options(cfg)["hook_types"])
            self.assertIn("broll_audio", variation_options(cfg)["visual_modes"])
            self.assertEqual(variation_options(cfg)["before_after_modes"], ["fullscreen"])
            self.assertEqual(len(variation_options(cfg)["product_broll"]["products"]), 6)
            self.assertEqual(loaded["variants"][0]["hook_type"], "transitional_hook")

    def test_none_hook_is_available_and_normalizes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = self._cfg(Path(temp_dir))

            loaded = normalize_profile(
                {
                    "variant_count": 1,
                    "variants": [{"name": "No opener", "hook_type": "none"}],
                },
                cfg,
            )

            self.assertEqual(variation_options(cfg)["hook_types"][0], "none")
            self.assertEqual(loaded["variants"][0]["hook_type"], "none")

    def test_save_normalizes_and_revision_conflict_protects_updates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = self._cfg(Path(temp_dir))
            profile = default_profile(cfg)
            profile["variant_count"] = 3
            profile["variants"][0]["letterbox_enabled"] = True
            profile["variants"][1]["letterbox_enabled"] = True
            profile["variants"][2]["letterbox_enabled"] = False
            profile["variants"][0]["hook_type"] = "pain"
            profile["variants"][1]["visual_mode"] = "broll_audio"
            profile["variants"][0]["random_broll_enabled"] = True
            profile["variants"][1]["random_broll_enabled"] = True
            profile["variants"][1]["subtitle_enabled"] = False
            profile["variants"][1]["mirror_enabled"] = True
            profile["variants"][1]["before_after_mode"] = "compact"
            profile["variants"][2]["product_zoom_enabled"] = False
            profile["variants"][0]["subtitle_y_frac"] = 0.5
            profile["variants"][1]["letterbox_top_frac"] = 0.12
            profile["variants"][1]["letterbox_bottom_frac"] = 0.28
            profile["variants"][1]["subtitle_size"] = "large"
            profile["variants"][1]["letterbox_hook_enabled"] = True
            profile["variants"][1]["letterbox_hook_font_id"] = profile["variants"][1]["font_id"]
            profile["variants"][1]["letterbox_hook_font_color"] = "#00AAFF"
            profile["variants"][1]["letterbox_hook_font_size"] = 999
            profile["variants"][1]["letterbox_hook_x_frac"] = 1.5
            profile["variants"][1]["letterbox_hook_y_frac"] = -1
            profile["variants"][1]["dynamic_text_settings"]["benefits"].update({
                "font_size": 999,
                "animation": "unknown",
                "duration_seconds": 9.25,
                "headline_font_id": "missing.ttf",
                "body_font_id": "missing.ttf",
            })
            profile["variants"][1]["dynamic_text_settings"]["cta"]["font_size"] = 2

            saved = save_active_profile(cfg, profile, expected_revision=default_profile(cfg)["revision"])
            loaded = load_active_profile(cfg)

            self.assertEqual(saved["revision"], loaded["revision"])
            self.assertEqual(
                [idx for idx, item in enumerate(loaded["variants"]) if item["letterbox_enabled"]],
                [0, 1],
            )
            self.assertEqual(loaded["variants"][0]["hook_type"], "text")
            self.assertEqual(loaded["variants"][1]["visual_mode"], "broll_audio")
            self.assertTrue(loaded["variants"][0]["random_broll_enabled"])
            self.assertFalse(loaded["variants"][1]["random_broll_enabled"])
            self.assertFalse(loaded["variants"][1]["subtitle_enabled"])
            self.assertTrue(loaded["variants"][1]["mirror_enabled"])
            self.assertEqual(loaded["variants"][1]["before_after_mode"], "fullscreen")
            self.assertFalse(loaded["variants"][2]["product_zoom_enabled"])
            self.assertEqual(loaded["variants"][0]["subtitle_y_frac"], 0.5)
            self.assertEqual(loaded["variants"][1]["letterbox_top_frac"], 0.12)
            self.assertEqual(loaded["variants"][1]["letterbox_bottom_frac"], 0.28)
            self.assertEqual(loaded["variants"][1]["subtitle_size"], "large")
            self.assertTrue(loaded["variants"][1]["letterbox_hook_enabled"])
            self.assertEqual(loaded["variants"][1]["letterbox_hook_font_id"], loaded["variants"][1]["font_id"])
            self.assertEqual(loaded["variants"][1]["letterbox_hook_font_color"], "#00AAFF")
            self.assertEqual(loaded["variants"][1]["letterbox_hook_font_size"], 160)
            self.assertEqual(loaded["variants"][1]["letterbox_hook_x_frac"], 1.0)
            self.assertEqual(loaded["variants"][1]["letterbox_hook_y_frac"], 0.0)
            benefit_settings = loaded["variants"][1]["dynamic_text_settings"]["benefits"]
            self.assertEqual(benefit_settings["font_size"], 72)
            self.assertEqual(benefit_settings["animation"], "current")
            self.assertEqual(benefit_settings["duration_seconds"], 6.0)
            self.assertEqual(benefit_settings["headline_font_id"], loaded["variants"][1]["headline_font_id"])
            self.assertEqual(benefit_settings["body_font_id"], loaded["variants"][1]["caption_font_id"])
            self.assertEqual(loaded["variants"][1]["dynamic_text_settings"]["cta"]["font_size"], 24)
            self.assertEqual(active_profile_revision(cfg), loaded["revision"])

            no_bars = dict(loaded)
            no_bars["variants"] = [
                dict(item, letterbox_enabled=False, letterbox_top_frac=0.0, letterbox_bottom_frac=0.0)
                for item in loaded["variants"]
            ]
            saved_no_bars = save_active_profile(cfg, no_bars, expected_revision=loaded["revision"])
            self.assertEqual(
                [idx for idx, item in enumerate(saved_no_bars["variants"]) if item["letterbox_enabled"]],
                [],
            )
            self.assertNotEqual(saved_no_bars["revision"], loaded["revision"])

            stale = dict(profile)
            stale["variant_count"] = 2
            with self.assertRaises(VariationRevisionConflict):
                save_active_profile(cfg, stale, expected_revision="stale")

    def test_preview_source_is_fixed_asset_and_does_not_scan_latest_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = self._cfg(Path(temp_dir))
            run_dir = Path(cfg.OUTPUT_DIR) / "run_001"
            run_dir.mkdir(parents=True)
            barred = run_dir / "v0_barred.mp4"
            clean = run_dir / "v1_clean.mp4"
            barred.touch()
            clean.touch()
            (run_dir / "manifest.json").write_text(
                """
[
  {
    "clip_id": "clip_0001_v0",
    "output_file": "v0_barred.mp4",
    "status": "ok",
    "letterbox_enabled": true
  },
  {
    "clip_id": "clip_0001_v1",
    "output_file": "v1_clean.mp4",
    "status": "ok",
    "letterbox_enabled": false
  }
]
""".strip(),
                encoding="utf-8",
            )

            fixed_source = Path(temp_dir) / "assets" / "variation_preview" / "raw_cut_preview.mp4"
            with mock.patch("variation_profile.FIXED_PREVIEW_SOURCE", fixed_source):
                source_ref = preview_source_ref(cfg)
                result = generate_previews(cfg, default_profile(cfg))

            self.assertEqual(source_ref["path"], str(fixed_source.resolve()))
            self.assertFalse(source_ref["exists"])
            self.assertEqual(result["source_clip"], str(fixed_source.resolve()))
            self.assertEqual(result["previews"], [])
            self.assertIn("Fixed preview clip", result["message"])


if __name__ == "__main__":
    unittest.main()
