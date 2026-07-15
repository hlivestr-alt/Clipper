import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from variation_engine import (
    _apply_variant_timeline_offsets,
    apply_variant_to_cfg,
    expand_moments_with_variants,
    generate_variants,
    resolve_variant_plan,
    VariantConfig,
)
from variation_profile import default_profile, save_active_profile


def _source_aware_transitional_cfg(root: Path, asset_count: int) -> SimpleNamespace:
    hook_dir = root / "transitional_hooks"
    hook_dir.mkdir()
    for index in range(asset_count):
        Path(hook_dir, f"viral_hook_{index:02d}.mp4").touch()
    cfg = SimpleNamespace(
        WORKING_DIR=str(root / "working"),
        OUTPUT_DIR=str(root / "output"),
        VARIANTS_PER_CLIP=6,
        HOOK_DURATION=1.5,
        HOOK_FONTSIZE=100,
        ZOOM_SCALE=1.45,
        SUBTITLE_Y_POS=0.80,
        FONT_SUBTITLE="assets/fonts/Montserrat-ExtraBold.ttf",
        FONT_HOOK="assets/fonts/Montserrat-ExtraBold.ttf",
        FONT_HOOK_FALLBACKS=[],
        KARAOKE_ACTIVE_COLOR="#FFD600",
        KARAOKE_INACTIVE_OPACITY=1.0,
        BROLL_INTRO_ENABLED=False,
        BGM_DIR=str(root / "bgm"),
        TRANSITIONAL_HOOK_ENABLED=True,
        TRANSITIONAL_HOOK_DIR=str(hook_dir),
    )
    profile = default_profile(cfg)
    profile["variants"][2]["name"] = "Transitional Hook"
    profile["variants"][2]["hook_type"] = "transitional_hook"
    profile["variants"][5]["name"] = "Transitional + BB"
    profile["variants"][5]["hook_type"] = "transitional_hook"
    profile["variants"][5]["letterbox_enabled"] = True
    save_active_profile(cfg, profile, expected_revision=default_profile(cfg)["revision"])
    return cfg


def _transitional_moments(count: int) -> list[dict]:
    return [
        {
            "clip_id": f"clip_{index:04d}",
            "start": float(index * 30),
            "end": float(index * 30 + 20),
            "score": 9,
            "hook": f"Hook {index}",
            "product": "Serum",
            "selected_text": f"source moment {index}",
        }
        for index in range(count)
    ]


class VariationGeneratorTests(unittest.TestCase):
    def test_global_audio_switches_cannot_be_reenabled_by_variant(self):
        base_cfg = SimpleNamespace(
            BGM_ENABLED=False,
            SFX_ENABLED=False,
            HOOK_DURATION=1.5,
            HOOK_FONTSIZE=100,
            ZOOM_SCALE=1.45,
            SUBTITLE_FONT_RANDOMIZE=False,
        )
        variant = VariantConfig(
            bgm_mode="selected",
            bgm_path="assets/bgm/focus.mp3",
            sfx_enabled=True,
        )
        patched = apply_variant_to_cfg(base_cfg, variant)
        self.assertFalse(patched.BGM_ENABLED)
        self.assertFalse(patched.SFX_ENABLED)
        self.assertFalse(hasattr(patched, "_bgm_path"))

    def test_saved_profile_respects_launcher_selection_modes(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cfg = SimpleNamespace(
                WORKING_DIR=str(root / "working"),
                VARIANTS_PER_CLIP=1,
                HOOK_DURATION=1.5,
                ZOOM_SCALE=1.45,
                SUBTITLE_Y_POS=0.80,
                FONT_SUBTITLE="assets/fonts/Montserrat-ExtraBold.ttf",
                FONT_HOOK="assets/fonts/Montserrat-ExtraBold.ttf",
                FONT_HOOK_FALLBACKS=[],
                KARAOKE_ACTIVE_COLOR="#FFD600",
                KARAOKE_INACTIVE_OPACITY=1.0,
                BROLL_INTRO_ENABLED=False,
                BGM_DIR=str(root / "bgm"),
            )
            profile_cfg_values = dict(vars(cfg))
            profile_cfg_values["VARIANTS_PER_CLIP"] = 6
            profile = default_profile(SimpleNamespace(**profile_cfg_values))
            save_active_profile(cfg, profile, expected_revision=default_profile(cfg)["revision"])

            for mode, requested, expected in (
                ("original", 6, 1),
                ("all", 1, 6),
                ("custom", 1, 1),
                ("custom", 2, 2),
                ("custom", 5, 5),
            ):
                variants, count, resolved_mode = resolve_variant_plan(
                    cfg,
                    n_variants=requested,
                    selection_mode=mode,
                )
                self.assertEqual(resolved_mode, mode)
                self.assertEqual(count, expected)
                self.assertEqual(len(variants or []), expected)

    def test_config_count_is_fallback_without_saved_profile(self):
        with TemporaryDirectory() as temp_dir:
            cfg = SimpleNamespace(WORKING_DIR=str(Path(temp_dir) / "working"), VARIANTS_PER_CLIP=4)
            variants, count, mode = resolve_variant_plan(cfg)
            self.assertEqual(count, 4)
            self.assertEqual(len(variants or []), 4)
            self.assertEqual(mode, "fallback")

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
        self.assertEqual(
            {variant.before_after_variant_mode for variant in variants},
            {"fullscreen"},
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

    def test_active_variation_profile_drives_expansion(self):
        with TemporaryDirectory() as tmp_dir:
            cfg = SimpleNamespace(
                WORKING_DIR=str(Path(tmp_dir) / "working"),
                OUTPUT_DIR=str(Path(tmp_dir) / "output"),
                VARIANTS_PER_CLIP=6,
                HOOK_DURATION=1.5,
                ZOOM_SCALE=1.45,
                SUBTITLE_Y_POS=0.80,
                FONT_SUBTITLE="assets/fonts/Montserrat-ExtraBold.ttf",
                FONT_HOOK="assets/fonts/Montserrat-ExtraBold.ttf",
                FONT_HOOK_FALLBACKS=[],
                KARAOKE_ACTIVE_COLOR="#FFD600",
                KARAOKE_INACTIVE_OPACITY=1.0,
                BROLL_INTRO_ENABLED=False,
                BGM_DIR=str(Path(tmp_dir) / "bgm"),
            )
            profile = default_profile(cfg)
            profile["variant_count"] = 2
            profile["variants"][0]["name"] = "Clean Control"
            profile["variants"][0]["hook_type"] = "text_b_roll"
            profile["variants"][0]["subtitle_enabled"] = False
            profile["variants"][0]["random_broll_enabled"] = True
            profile["variants"][1]["name"] = "Bar Variant"
            profile["variants"][1]["visual_mode"] = "broll_audio"
            profile["variants"][1]["random_broll_enabled"] = True
            profile["variants"][1]["mirror_enabled"] = True
            profile["variants"][1]["before_after_mode"] = "compact"
            profile["variants"][1]["letterbox_enabled"] = True
            profile["variants"][1]["letterbox_top_frac"] = 0.11
            profile["variants"][1]["letterbox_bottom_frac"] = 0.27
            profile["variants"][1]["subtitle_y_frac"] = 0.57
            profile["variants"][1]["subtitle_size"] = "small"
            profile["variants"][1]["letterbox_hook_enabled"] = True
            profile["variants"][1]["letterbox_hook_font_color"] = "#FFEE00"
            profile["variants"][1]["letterbox_hook_font_size"] = 88
            profile["variants"][1]["letterbox_hook_x_frac"] = 0.42
            profile["variants"][1]["letterbox_hook_y_frac"] = 0.64
            profile["variants"][1]["zoom_intensity"] = "none"
            profile["variants"][1]["product_zoom_enabled"] = False
            saved = save_active_profile(cfg, profile, expected_revision=default_profile(cfg)["revision"])
            moments = [{
                "clip_id": "clip_0001",
                "start": 10.0,
                "end": 40.0,
                "score": 9,
                "hook": "Serum best seller",
                "product": "Serum",
                "selected_text": "pakai serum proya ini",
            }]

            expanded = expand_moments_with_variants(moments, cfg, n_variants=6, seed=42)

            self.assertEqual(len(expanded), 2)
            self.assertEqual(expanded[0]["_variant"].display_name, "Clean Control")
            self.assertEqual(expanded[0]["_variant"].hook_type, "text_b_roll")
            self.assertFalse(expanded[0]["_variant"].subtitle_enabled)
            self.assertTrue(expanded[0]["_variant"].random_broll_enabled)
            self.assertEqual(expanded[0]["_base_clip_id"], "clip_0001")
            self.assertEqual(expanded[0]["_variant"].profile_revision, saved["revision"])
            self.assertTrue(expanded[1]["_variant"].letterbox_enabled)
            self.assertEqual(expanded[1]["_variant"].visual_mode, "broll_audio")
            self.assertFalse(expanded[1]["_variant"].random_broll_enabled)
            self.assertTrue(expanded[1]["_variant"].mirror)
            self.assertEqual(expanded[1]["_variant"].before_after_variant_mode, "fullscreen")
            self.assertEqual(expanded[1]["_variant"].speed_ramp, 1.0)
            self.assertEqual(expanded[1]["_variant"].crop_x_offset, 0.0)
            self.assertEqual(expanded[1]["_variant"].start_offset_seconds, 0.0)
            self.assertEqual(expanded[1]["_variant"].end_offset_seconds, 0.0)
            self.assertEqual(expanded[1]["_variant"].letterbox_top_frac, 0.11)
            self.assertEqual(expanded[1]["_variant"].letterbox_bottom_frac, 0.27)
            self.assertEqual(expanded[1]["_variant"].subtitle_y_frac, 0.57)
            self.assertEqual(expanded[1]["_variant"].subtitle_size, "small")
            self.assertEqual(expanded[1]["_variant"].subtitle_font_size, 96)
            self.assertTrue(expanded[1]["_variant"].letterbox_hook_enabled)
            self.assertEqual(expanded[1]["_variant"].letterbox_hook_font_color, "#FFEE00")
            self.assertEqual(expanded[1]["_variant"].letterbox_hook_font_size, 88)
            self.assertEqual(expanded[1]["_variant"].letterbox_hook_x_frac, 0.42)
            self.assertEqual(expanded[1]["_variant"].letterbox_hook_y_frac, 0.64)
            self.assertEqual(expanded[1]["_variant"].zoom_intensity, "none")
            self.assertFalse(expanded[1]["_variant"].product_zoom_enabled)

    def test_default_variation_profile_drives_expansion_when_no_profile_saved(self):
        with TemporaryDirectory() as tmp_dir:
            cfg = SimpleNamespace(
                WORKING_DIR=str(Path(tmp_dir) / "working"),
                OUTPUT_DIR=str(Path(tmp_dir) / "output"),
                VARIANTS_PER_CLIP=2,
                HOOK_DURATION=1.5,
                ZOOM_SCALE=1.45,
                SUBTITLE_Y_POS=0.80,
                FONT_SUBTITLE="assets/fonts/Montserrat-ExtraBold.ttf",
                FONT_HOOK="assets/fonts/Montserrat-ExtraBold.ttf",
                FONT_HOOK_FALLBACKS=[],
                KARAOKE_ACTIVE_COLOR="#FFD600",
                KARAOKE_INACTIVE_OPACITY=1.0,
                BROLL_INTRO_ENABLED=False,
                BGM_DIR=str(Path(tmp_dir) / "bgm"),
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

            expanded = expand_moments_with_variants(moments, cfg, n_variants=6, seed=42)

            self.assertEqual(len(expanded), 2)
            self.assertEqual(expanded[0]["_variant"].display_name, "Original")
            self.assertEqual(expanded[1]["_variant"].display_name, "Before After")
            self.assertEqual(expanded[1]["_variant"].font_subtitle, "assets/fonts/Montserrat-ExtraBold.ttf")
            self.assertEqual(expanded[1]["_variant"].subtitle_y_frac, 0.58)
            self.assertEqual(expanded[1]["_variant"].before_after_variant_mode, "fullscreen")
            self.assertTrue(expanded[1]["_variant"].profile_revision)

    def test_profile_broll_intro_can_use_detected_product_fallback(self):
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            toner_dir = root / "broll_intro" / "Toner"
            serum_dir = root / "broll_intro" / "Serum"
            toner_dir.mkdir(parents=True)
            serum_dir.mkdir(parents=True)
            Path(toner_dir, "toner_intro.mp4").touch()
            Path(serum_dir, "serum_intro.mp4").touch()
            cfg = SimpleNamespace(
                WORKING_DIR=str(root / "working"),
                OUTPUT_DIR=str(root / "output"),
                VARIANTS_PER_CLIP=1,
                HOOK_DURATION=1.5,
                ZOOM_SCALE=1.45,
                SUBTITLE_Y_POS=0.80,
                FONT_SUBTITLE="assets/fonts/Montserrat-ExtraBold.ttf",
                FONT_HOOK="assets/fonts/Montserrat-ExtraBold.ttf",
                FONT_HOOK_FALLBACKS=[],
                KARAOKE_ACTIVE_COLOR="#FFD600",
                KARAOKE_INACTIVE_OPACITY=1.0,
                PRODUCT_CLASSES={3: "Serum", 5: "Toner"},
                BROLL_INTRO_ENABLED=True,
                BROLL_INTRO_DIR=str(root / "broll_intro"),
                BROLL_INTRO_MAX_DURATION=2.5,
                BROLL_INTRO_REQUIRE_PRODUCT_MATCH=True,
                BROLL_INTRO_ALLOW_GENERIC_ROOT=False,
                BROLL_INTRO_PRODUCT_ALIASES={"Serum": ["serum"], "Toner": ["toner"]},
                BGM_DIR=str(root / "bgm"),
            )
            profile = default_profile(cfg)
            profile["variant_count"] = 1
            profile["variants"][0]["name"] = "B-Roll Hook"
            profile["variants"][0]["hook_type"] = "b_roll"
            profile["variants"][0]["visual_mode"] = "host"
            save_active_profile(cfg, profile, expected_revision=default_profile(cfg)["revision"])
            moments = [{
                "clip_id": "clip_0005",
                "start": 520.52,
                "end": 554.29,
                "score": 9,
                "hook": "BIAR KULIT TAMPAK FRESH",
                "product": "PROYA 5X Vitamin C",
                "selected_text": "harga normalnya turun hari ini",
                "_detected_product_key": "toner",
            }]

            expanded = expand_moments_with_variants(moments, cfg, n_variants=1, seed=42)
            variant = expanded[0]["_variant"]

            self.assertTrue(variant.broll_intro_enabled)
            self.assertEqual(Path(variant.broll_intro_path).parent, toner_dir)
            self.assertEqual(variant.broll_intro_product, "toner")

    def test_profile_transitional_hook_assigns_pre_roll_asset(self):
        with TemporaryDirectory() as tmp_dir:
            hook_dir = Path(tmp_dir) / "transitional_hooks"
            hook_dir.mkdir()
            Path(hook_dir, "viral_hook_a.mp4").touch()
            Path(hook_dir, "viral_hook_b.mov").touch()
            cfg = SimpleNamespace(
                WORKING_DIR=str(Path(tmp_dir) / "working"),
                OUTPUT_DIR=str(Path(tmp_dir) / "output"),
                VARIANTS_PER_CLIP=1,
                HOOK_DURATION=1.5,
                HOOK_FONTSIZE=100,
                ZOOM_SCALE=1.45,
                SUBTITLE_Y_POS=0.80,
                FONT_SUBTITLE="assets/fonts/Montserrat-ExtraBold.ttf",
                FONT_HOOK="assets/fonts/Montserrat-ExtraBold.ttf",
                FONT_HOOK_FALLBACKS=[],
                KARAOKE_ACTIVE_COLOR="#FFD600",
                KARAOKE_INACTIVE_OPACITY=1.0,
                BROLL_INTRO_ENABLED=False,
                BGM_DIR=str(Path(tmp_dir) / "bgm"),
                TRANSITIONAL_HOOK_ENABLED=True,
                TRANSITIONAL_HOOK_DIR=str(hook_dir),
            )
            profile = default_profile(cfg)
            profile["variant_count"] = 1
            profile["variants"][0]["name"] = "Transitional Hook"
            profile["variants"][0]["hook_type"] = "transitional_hook"
            save_active_profile(cfg, profile, expected_revision=default_profile(cfg)["revision"])
            moments = [{
                "clip_id": "clip_0001",
                "start": 10.0,
                "end": 40.0,
                "score": 9,
                "hook": "Serum best seller",
                "product": "Serum",
                "selected_text": "pakai serum proya ini",
            }]

            expanded = expand_moments_with_variants(moments, cfg, n_variants=1, seed=42)
            variant = expanded[0]["_variant"]
            patched = apply_variant_to_cfg(cfg, variant)

            self.assertTrue(variant.transitional_hook_enabled)
            self.assertEqual(Path(variant.transitional_hook_path).parent, hook_dir)
            self.assertEqual(variant.hook_type, "transitional_hook")
            self.assertEqual(variant.hook_duration, 0.0)
            self.assertEqual(patched._transitional_hook_path, variant.transitional_hook_path)
            self.assertEqual(patched._hook_format, "transitional_hook")
            self.assertEqual(patched.HOOK_DURATION, 0.0)

    def test_source_aware_transitional_hooks_are_stable_distinct_and_non_repeating(self):
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            cfg = _source_aware_transitional_cfg(root, asset_count=6)
            moments = _transitional_moments(3)

            first = expand_moments_with_variants(
                moments,
                cfg,
                n_variants=6,
                seed=42,
                source_identity=str(root / "source_a.mp4"),
            )
            repeat = expand_moments_with_variants(
                _transitional_moments(3),
                cfg,
                n_variants=6,
                seed=42,
                source_identity=str(root / "SOURCE_A.mp4"),
            )
            other_source = expand_moments_with_variants(
                _transitional_moments(3),
                cfg,
                n_variants=6,
                seed=42,
                source_identity=str(root / "source_b.mp4"),
            )

            first_paths = [
                item["_variant"].transitional_hook_path
                for item in first
                if item["_variant"].transitional_hook_path
            ]
            repeat_paths = [
                item["_variant"].transitional_hook_path
                for item in repeat
                if item["_variant"].transitional_hook_path
            ]
            other_paths = [
                item["_variant"].transitional_hook_path
                for item in other_source
                if item["_variant"].transitional_hook_path
            ]
            self.assertEqual(first_paths, repeat_paths)
            self.assertNotEqual(first_paths, other_paths)
            self.assertEqual(len(first_paths), len(set(first_paths)))
            self.assertEqual(
                {
                    item["_variant"].variant_id
                    for item in first
                    if item["_variant"].transitional_hook_path
                },
                {"v2_transitional_hook", "v5_transitional_bb"},
            )

    def test_source_aware_transitional_hook_pool_reshuffles_without_boundary_repeat(self):
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            cfg = _source_aware_transitional_cfg(root, asset_count=3)
            expanded = expand_moments_with_variants(
                _transitional_moments(4),
                cfg,
                n_variants=6,
                seed=42,
                source_identity=str(root / "source.mp4"),
            )

            paths = [
                item["_variant"].transitional_hook_path
                for item in expanded
                if item["_variant"].transitional_hook_path
            ]
            self.assertEqual(len(set(paths[0:3])), 3)
            self.assertEqual(len(set(paths[3:6])), 3)
            self.assertNotEqual(paths[2], paths[3])

    def test_profile_none_hook_disables_opening_hook_duration(self):
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            cfg = SimpleNamespace(
                WORKING_DIR=str(root / "working"),
                OUTPUT_DIR=str(root / "output"),
                VARIANTS_PER_CLIP=1,
                HOOK_DURATION=1.5,
                HOOK_FONTSIZE=100,
                ZOOM_SCALE=1.45,
                SUBTITLE_Y_POS=0.80,
                FONT_SUBTITLE="assets/fonts/Montserrat-ExtraBold.ttf",
                FONT_HOOK="assets/fonts/Montserrat-ExtraBold.ttf",
                FONT_HOOK_FALLBACKS=[],
                KARAOKE_ACTIVE_COLOR="#FFD600",
                KARAOKE_INACTIVE_OPACITY=1.0,
                BROLL_INTRO_ENABLED=False,
                BGM_DIR=str(root / "bgm"),
                TRANSITIONAL_HOOK_ENABLED=False,
            )
            profile = default_profile(cfg)
            profile["variant_count"] = 1
            profile["variants"][0]["name"] = "No Hook"
            profile["variants"][0]["hook_type"] = "none"
            save_active_profile(cfg, profile, expected_revision=default_profile(cfg)["revision"])
            moments = [{
                "clip_id": "clip_0001",
                "start": 10.0,
                "end": 40.0,
                "score": 9,
                "hook": "Serum best seller",
                "product": "Serum",
                "selected_text": "pakai serum proya ini",
            }]

            expanded = expand_moments_with_variants(moments, cfg, n_variants=1, seed=42)
            variant = expanded[0]["_variant"]
            patched = apply_variant_to_cfg(cfg, variant)

            self.assertEqual(variant.hook_type, "none")
            self.assertEqual(variant.hook_duration, 0.0)
            self.assertEqual(patched._hook_format, "none")
            self.assertEqual(patched.HOOK_DURATION, 0.0)

    def test_broll_audio_visual_mode_skips_legacy_intro_and_transitional_hook(self):
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            broll_intro_dir = root / "broll_intro" / "serum"
            broll_intro_dir.mkdir(parents=True)
            Path(broll_intro_dir, "intro.mp4").touch()
            hook_dir = root / "transitional_hooks"
            hook_dir.mkdir()
            Path(hook_dir, "viral_hook.mp4").touch()
            cfg = SimpleNamespace(
                WORKING_DIR=str(root / "working"),
                OUTPUT_DIR=str(root / "output"),
                VARIANTS_PER_CLIP=2,
                HOOK_DURATION=1.5,
                HOOK_FONTSIZE=100,
                ZOOM_SCALE=1.45,
                SUBTITLE_Y_POS=0.80,
                FONT_SUBTITLE="assets/fonts/Montserrat-ExtraBold.ttf",
                FONT_HOOK="assets/fonts/Montserrat-ExtraBold.ttf",
                FONT_HOOK_FALLBACKS=[],
                KARAOKE_ACTIVE_COLOR="#FFD600",
                KARAOKE_INACTIVE_OPACITY=1.0,
                BGM_DIR=str(root / "bgm"),
                BROLL_INTRO_ENABLED=True,
                BROLL_INTRO_DIR=str(root / "broll_intro"),
                BROLL_INTRO_REQUIRE_PRODUCT_MATCH=True,
                TRANSITIONAL_HOOK_ENABLED=True,
                TRANSITIONAL_HOOK_DIR=str(hook_dir),
            )
            profile = default_profile(cfg)
            profile["variant_count"] = 2
            profile["variants"][0]["hook_type"] = "text_b_roll"
            profile["variants"][0]["visual_mode"] = "broll_audio"
            profile["variants"][1]["hook_type"] = "transitional_hook"
            profile["variants"][1]["visual_mode"] = "broll_audio"
            save_active_profile(cfg, profile, expected_revision=default_profile(cfg)["revision"])
            moments = [{
                "clip_id": "clip_0001",
                "start": 10.0,
                "end": 40.0,
                "score": 9,
                "hook": "Serum best seller",
                "product": "Serum",
                "selected_text": "pakai serum proya ini",
            }]

            expanded = expand_moments_with_variants(moments, cfg, n_variants=2, seed=42)

            variants = [item["_variant"] for item in expanded]
            self.assertTrue(all(variant.visual_mode == "broll_audio" for variant in variants))
            self.assertTrue(all(not variant.broll_intro_enabled for variant in variants))
            self.assertTrue(all(not variant.transitional_hook_enabled for variant in variants))

    def test_apply_variant_to_cfg_sets_profile_render_overrides(self):
        base_cfg = SimpleNamespace(
            BGM_ENABLED=True,
            SFX_ENABLED=True,
            HOOK_DURATION=1.5,
            HOOK_FONTSIZE=100,
            ZOOM_SCALE=1.45,
            SUBTITLE_FONT_RANDOMIZE=True,
        )
        variant = VariantConfig(
            variant_id="v1_test",
            variant_index=1,
            font_subtitle="assets/fonts/Anton-Regular.ttf",
            subtitle_base_color="#EFEFEF",
            karaoke_active_color="#00D4FF",
            hook_color="#EFEFEF",
            highlight_color="#00D4FF",
            bgm_mode="selected",
            bgm_path="assets/bgm/focus.mp3",
            sfx_enabled=False,
            zoom_intensity="none",
            product_zoom_enabled=False,
            subtitle_enabled=False,
            visual_mode="broll_audio",
            letterbox_enabled=True,
            subtitle_y_frac=0.57,
            subtitle_size="large",
            subtitle_font_size=144,
            letterbox_top_frac=0.11,
            letterbox_bottom_frac=0.27,
            letterbox_hook_enabled=True,
            letterbox_hook_font_id="assets/fonts/Montserrat-ExtraBold.ttf",
            letterbox_hook_font_color="#FFEE00",
            letterbox_hook_font_size=88,
            letterbox_hook_x_frac=0.42,
            letterbox_hook_y_frac=0.64,
            mirror=True,
            before_after_variant_mode="compact",
            hook_type="text_before_after_image",
        )

        patched = apply_variant_to_cfg(base_cfg, variant)

        self.assertEqual(patched.FONT_SUBTITLE, "assets/fonts/Anton-Regular.ttf")
        self.assertFalse(patched.SUBTITLE_FONT_RANDOMIZE)
        self.assertEqual(patched.SUBTITLE_BASE_COLOR, "#EFEFEF")
        self.assertEqual(patched.KARAOKE_ACTIVE_COLOR, "#00D4FF")
        self.assertTrue(patched.BGM_ENABLED)
        self.assertEqual(patched._bgm_path, "assets/bgm/focus.mp3")
        self.assertFalse(patched.SFX_ENABLED)
        self.assertTrue(patched._zoom_disabled)
        self.assertFalse(patched._product_zoom_enabled)
        self.assertFalse(patched._subtitle_enabled)
        self.assertEqual(patched._visual_mode, "broll_audio")
        self.assertTrue(patched._mirror)
        self.assertEqual(patched._before_after_variant_mode, "fullscreen")
        self.assertTrue(patched._letterbox_enabled)
        self.assertEqual(patched._variant_subtitle_y_frac, 0.57)
        self.assertEqual(patched.SUBTITLE_FONTSIZE, 144)
        self.assertEqual(patched._variant_subtitle_size, "large")
        self.assertEqual(patched._variant_subtitle_font_size, 144)
        self.assertEqual(patched._letterbox_top_frac, 0.11)
        self.assertEqual(patched._letterbox_bottom_frac, 0.27)
        self.assertTrue(patched._letterbox_hook_enabled)
        self.assertEqual(patched._letterbox_hook_font_id, "assets/fonts/Montserrat-ExtraBold.ttf")
        self.assertEqual(patched._letterbox_hook_font_color, "#FFEE00")
        self.assertEqual(patched._letterbox_hook_font_size, 88)
        self.assertEqual(patched._letterbox_hook_x_frac, 0.42)
        self.assertEqual(patched._letterbox_hook_y_frac, 0.64)
        self.assertEqual(patched._hook_format, "text_before_after_image")

        random_variant = VariantConfig(visual_mode="host", random_broll_enabled=True)
        random_patched = apply_variant_to_cfg(base_cfg, random_variant)
        self.assertTrue(random_patched._random_broll_enabled)


if __name__ == "__main__":
    unittest.main()
