import re
import types
import unittest
from pathlib import Path
from unittest import mock

from ffmpeg_editor import (
    _add_before_after_overlay_filters,
    _add_letterbox_hook_text,
    _add_product_broll_visual_filters,
    _add_random_broll_cutaway_filters,
    _add_transitional_hook_concat_filters,
    _build_and_run,
    _build_zoom_expressions,
    _cpu_encode_fallback_cmd,
    _hook_layout_settings,
    _letterbox_bar_heights,
    _prepare_karaoke_words,
    _resolve_subtitle_font,
    _subtitle_line_centers,
    _subtitle_row_centers,
    _variant_hook_format,
    _write_ass_file,
)
from product_broll import BrollClip, BrollPlan, BrollTransition, RandomBrollPlan, RandomBrollSegment


class FfmpegEditorFallbackTests(unittest.TestCase):
    def test_cpu_encode_fallback_replaces_nvenc_options(self):
        cfg = types.SimpleNamespace(OUTPUT_CRF=24)
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            "in.mp4",
            "-c:v",
            "h264_nvenc",
            "-preset",
            "p4",
            "-cq",
            "26",
            "-rc",
            "vbr",
            "-b:v",
            "0",
            "-c:a",
            "aac",
            "out.mp4",
        ]

        fallback = _cpu_encode_fallback_cmd(cmd, cfg)

        self.assertIn("libx264", fallback)
        self.assertNotIn("h264_nvenc", fallback)
        self.assertNotIn("-cq", fallback)
        self.assertNotIn("-rc", fallback)
        self.assertNotIn("-b:v", fallback)
        self.assertEqual(fallback[fallback.index("-preset") + 1], "fast")
        self.assertEqual(fallback[fallback.index("-crf") + 1], "24")
        self.assertEqual(fallback[-1], "out.mp4")

    def test_before_after_hook_format_uses_opening_window(self):
        cfg = types.SimpleNamespace(HOOK_DURATION=2.5, BEFORE_AFTER_DURATION=2.5, BEFORE_AFTER_OPACITY=1.0)
        fc = []

        result = _add_before_after_overlay_filters(
            fc=fc,
            vid="[v0]",
            extra_inputs=[{"path": "before.png", "type": "ba"}],
            clip_duration=20.0,
            W=1080,
            H=1920,
            cfg=cfg,
            hook_format="text_before_after_image",
        )

        self.assertEqual(result, "[vba]")
        joined = ";".join(fc)
        self.assertIn("scale=1080:1920:force_original_aspect_ratio=increase", joined)
        self.assertIn("crop=1080:1920", joined)
        self.assertIn("overlay=x='0':y='0'", joined)
        self.assertIn("between(t,0.00,", joined)

    def test_visual_hook_format_normalizes_legacy_values_to_text(self):
        cfg = types.SimpleNamespace(_hook_format="pain")

        self.assertEqual(_variant_hook_format(cfg), "text")
        self.assertEqual(_variant_hook_format(cfg, "none"), "none")
        self.assertEqual(_variant_hook_format(cfg, "text_b_roll"), "text_b_roll")
        self.assertEqual(_variant_hook_format(cfg, "transitional_hook"), "transitional_hook")

    def test_profile_driven_subtitles_do_not_randomize_font(self):
        cfg = types.SimpleNamespace(
            FONT_SUBTITLE="assets/fonts/Montserrat-ExtraBold.ttf",
            SUBTITLE_FONT_RANDOMIZE=True,
            SUBTITLE_FONT_DIR="assets/fonts/subtitle",
            _variation_profile_driven=True,
        )

        with mock.patch("ffmpeg_editor._existing_dir", side_effect=AssertionError("should not scan random fonts")):
            font_name, font_dir = _resolve_subtitle_font(cfg)

        self.assertEqual(font_name, "Montserrat ExtraBold")
        self.assertIsNone(font_dir)

    def test_transitional_hook_concat_prepends_video_without_overlay(self):
        fc = []

        vid, aud = _add_transitional_hook_concat_filters(
            fc=fc,
            vid="[vmain]",
            aud="[amain]",
            transitional_input_idx=3,
            transitional_hook={"duration": 3.25, "has_audio": False},
            W=1080,
            H=1920,
            output_fps=30,
        )

        self.assertEqual(vid, "[vtransout]")
        self.assertEqual(aud, "[atransout]")
        joined = ";".join(fc)
        self.assertIn("[3:v]setpts=PTS-STARTPTS", joined)
        self.assertIn("atrim=0:3.250", joined)
        self.assertIn("concat=n=2:v=1:a=1[vtransout][atransout]", joined)
        self.assertNotIn("overlay=", joined)

    def test_face_zoom_can_render_without_product_zoom_trigger(self):
        expressions = _build_zoom_expressions(
            prod_trigger=None,
            face_zooms=[{"start": 1.0, "end": 2.5, "cx": 0.5, "cy": 0.3, "scale": 1.25}],
            clip_duration=8.0,
            W=1080,
            H=1920,
            zoom_dur=3.0,
            zoom_scale=1.45,
            timeline_fps=30.0,
        )

        self.assertIsNotNone(expressions)

    def test_letterbox_subtitle_position_honors_explicit_y_fraction(self):
        cfg = types.SimpleNamespace(
            _letterbox_enabled=True,
            _variant_subtitle_position="bottom",
            _variant_subtitle_y_frac=0.50,
            LETTERBOX_BAR_HEIGHT_FRAC=0.20,
        )

        y_line1, y_line2, _line_gap = _subtitle_line_centers(1920, 102, 0.50, cfg)

        self.assertLess(y_line2, int(1920 * 0.65))
        self.assertGreater(y_line1, int(1920 * 0.35))

    def test_letterbox_hook_layout_uses_independent_bar_bands(self):
        cfg = types.SimpleNamespace(
            _letterbox_enabled=True,
            _hook_layout_mode="standard",
            _letterbox_top_frac=0.10,
            _letterbox_bottom_frac=0.30,
        )

        layout = _hook_layout_settings(cfg, 1080, 1920)

        self.assertLess(layout["top_y"], int(1920 * 0.10))
        self.assertLess(layout["mid_y"], int(1920 * 0.10))
        self.assertGreater(layout["bottom_y"], int(1920 * 0.70))

    def test_letterbox_bar_heights_allow_zero_and_clamp_each_side(self):
        cfg = types.SimpleNamespace(
            _letterbox_enabled=True,
            _letterbox_top_frac=0.0,
            _letterbox_bottom_frac=0.80,
        )

        self.assertEqual(_letterbox_bar_heights(1000, cfg), (0, 400))

    def test_three_row_subtitle_centers_use_equal_spacing(self):
        centers, gap = _subtitle_row_centers(1920, 102, 0.84, types.SimpleNamespace(), 3)

        self.assertEqual(len(centers), 3)
        self.assertEqual(centers[1] - centers[0], gap)
        self.assertEqual(centers[2] - centers[1], gap)

    def test_karaoke_words_preserve_space_for_hyphenated_repetition(self):
        words = [{"word": "bener-bener", "start": 0.0, "end": 0.8}]

        prepared = _prepare_karaoke_words(words)

        self.assertEqual(prepared[0]["word"], "bener bener")

    def test_ass_subtitles_disable_auto_wrap_and_emit_three_explicit_rows(self):
        cfg = types.SimpleNamespace(
            FONT_SUBTITLE="assets/fonts/Montserrat-ExtraBold.ttf",
            SUBTITLE_FONT_RANDOMIZE=False,
            SUBTITLE_FONT_DIR="assets/fonts/subtitle",
            SUBTITLE_FONTSIZE=120,
            SUBTITLE_Y_POS=0.84,
            SUBTITLE_STROKE_W=3,
            SUBTITLE_STROKE="#000000",
            SUBTITLE_BASE_COLOR="#FFFFFF",
            KARAOKE_INACTIVE_OPACITY=1.0,
            KARAOKE_ACTIVE_COLOR="#FFD600",
        )
        words = [
            {"word": "supercalmingserum", "start": 0.0, "end": 1.0},
            {"word": "hydratingbrightener", "start": 1.0, "end": 2.0},
            {"word": "glowingcomplexion", "start": 2.0, "end": 3.0},
            {"word": "recommended", "start": 3.0, "end": 4.0},
        ]

        ass_path, _fonts_dir = _write_ass_file(words, [None] * len(words), 4.0, 1080, 1920, cfg)
        try:
            content = Path(ass_path).read_text(encoding="utf-8")
        finally:
            Path(ass_path).unlink(missing_ok=True)

        self.assertIn("WrapStyle: 2", content)
        y_positions = sorted({int(match.group(1)) for match in re.finditer(r"\\pos\(540,(\d+)\)", content)})
        self.assertEqual(len(y_positions), 3)
        self.assertEqual(y_positions[1] - y_positions[0], y_positions[2] - y_positions[1])

    def test_letterbox_hook_drawtext_requires_enabled_top_bar_and_text(self):
        cfg = types.SimpleNamespace(
            _letterbox_enabled=True,
            _letterbox_top_frac=0.20,
            _letterbox_bottom_frac=0.20,
            _letterbox_hook_enabled=True,
            _letterbox_hook_font_id="",
            _letterbox_hook_font_color="#FFEE00",
            _letterbox_hook_font_size=88,
            _letterbox_hook_x_frac=0.5,
            _letterbox_hook_y_frac=0.5,
            FONT_HOOK="",
            FONT_SUBTITLE="",
        )
        moment = {
            "hook_overlay": {
                "headline": "Flash sale",
                "subtext": "Dipakai rutin",
                "cta": "Cek produknya",
            }
        }
        fc = []

        out = _add_letterbox_hook_text(fc, "[v0]", "vhook", 8.0, 1080, 1920, moment, cfg)

        self.assertEqual(out, "[vhook]")
        self.assertIn("drawtext=text='FLASH SALE'", fc[0])
        self.assertIn("enable='between(t,0.00,8.00)'", fc[0])

        no_top_bar = types.SimpleNamespace(**{**vars(cfg), "_letterbox_top_frac": 0.0})
        fc = []
        out = _add_letterbox_hook_text(fc, "[v0]", "vhook", 8.0, 1080, 1920, moment, no_top_bar)
        self.assertEqual(out, "[v0]")
        self.assertEqual(fc, [])

    def test_product_broll_visual_graph_scales_crossfades_and_maps_raw_audio(self):
        plan = BrollPlan(
            product_key="serum",
            product_label="Serum",
            folder="assets/product_broll/serum",
            clips=(
                BrollClip(path="a.mp4", duration=1.2),
                BrollClip(path="b.mp4", duration=1.4),
            ),
            transitions=(BrollTransition(offset=0.9, duration=0.3),),
            target_duration=2.0,
            crossfade=0.3,
        )
        fc = []

        label = _add_product_broll_visual_filters(
            fc=fc,
            broll_input_indices=[1, 2],
            broll_plan=plan,
            clip_duration=2.0,
            W=1080,
            H=1920,
            output_fps=30,
        )

        joined = ";".join(fc)
        self.assertEqual(label, "[vbrollbase]")
        self.assertIn("scale=1080:1920:force_original_aspect_ratio=increase", joined)
        self.assertIn("crop=1080:1920", joined)
        self.assertIn("xfade=transition=fade:duration=0.300:offset=0.900", joined)
        self.assertIn("trim=0:2.000,setpts=PTS-STARTPTS[vbrollbase]", joined)
        self.assertNotIn(":a]", joined)

        cfg = types.SimpleNamespace(
            _variant_transforms_baked=False,
            _visual_mode="broll_audio",
            _variation_profile_driven=True,
            _letterbox_enabled=False,
            OUTPUT_CODEC="libx264",
            OUTPUT_PRESET="fast",
            OUTPUT_CRF=23,
            OUTPUT_AUDIO_BITRATE="128k",
            OUTPUT_FPS=30,
        )
        with mock.patch("ffmpeg_editor._run_ffmpeg", return_value=True) as run_ffmpeg:
            ok = _build_and_run(
                raw_clip_path="raw.mp4",
                output_path="out.mp4",
                ass_path=None,
                ass_fonts_dir=None,
                W=1080,
                H=1920,
                clip_duration=2.0,
                clip_fps=30,
                has_audio=True,
                moment={},
                prod_trigger=None,
                face_zooms=[],
                zoom_dur=3.0,
                zoom_scale=1.45,
                hook_end=0.0,
                extra_inputs=[],
                sfx_events=[],
                bgm_path=None,
                hook_format="text",
                broll_visual_plan=plan,
                cfg=cfg,
            )

        self.assertTrue(ok)
        cmd = run_ffmpeg.call_args.args[0]
        cmd_text = " ".join(str(item) for item in cmd)
        self.assertIn("-map 0:a", cmd_text)
        self.assertIn("xfade=transition=fade", cmd_text)
        self.assertNotIn("[1:a]", cmd_text)

    def test_random_broll_cutaway_graph_uses_source_offsets_and_full_frame_overlay(self):
        plan = RandomBrollPlan(
            product_key="serum",
            product_label="Serum",
            folder="assets/product_broll/serum",
            segments=(RandomBrollSegment("serum.mp4", start=4.25, duration=2.5, source_start=1.75),),
        )
        fc = ["[0:v]drawbox=x=0:y=0:w=1080:h=200:color=black:t=fill[vletterboxtext]"]

        output = _add_random_broll_cutaway_filters(fc, "[vletterboxtext]", [1], plan, 1080, 1920, 30)

        joined = ";".join(fc)
        self.assertEqual(output, "[vrbroll0]")
        self.assertIn("trim=start=1.750:end=4.250", joined)
        self.assertIn("scale=1080:1920:force_original_aspect_ratio=increase", joined)
        self.assertIn("[vletterboxtext][vrbrollsrc0]overlay", joined)
        self.assertIn("between(t,4.250,6.750)", joined)
        self.assertNotIn("[1:a]", joined)


if __name__ == "__main__":
    unittest.main()
