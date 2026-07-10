import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from main import _build_clip_job, _completed_resume_rows, _render_fingerprint
from variation_profile import default_profile, save_active_profile


class RenderResumeTests(unittest.TestCase):
    def test_render_fingerprint_tracks_each_render_setting_group(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cfg = SimpleNamespace(
                WORKING_DIR=str(root / "working"),
                OUTPUT_DIR=str(root / "output"),
                VARIANTS_PER_CLIP=1,
                OUTPUT_CODEC="h264_nvenc",
                OUTPUT_CQ=26,
                OUTPUT_NVENC_PRESET="p4",
                FONT_SUBTITLE="",
                FONT_HOOK="",
                FONT_HOOK_FALLBACKS=[],
                SUBTITLE_FONT_DIR="",
                BGM_DIR="",
                PRODUCT_BROLL_DIR="",
                BGM_VOLUME=0.08,
                BGM_DUCKING_RATIO=8.0,
                SFX_VOLUME_PRODUCT=0.15,
                BEFORE_AFTER_START_OFFSET=3.0,
                CTA_ENDCARD_ENABLED=True,
                SUBTITLE_SAFE_ZONE_BOTTOM=0.15,
                FACE_ZOOM_MIN_GAP=1.0,
                TRANSITIONAL_HOOK_ENABLED=True,
                QUEUE_POLL_INTERVAL=2.0,
            )
            baseline = _render_fingerprint("missing.mp4", cfg, max_clips=None, cut_only=False)
            changes = {
                "OUTPUT_NVENC_PRESET": "p5",
                "BGM_VOLUME": 0.2,
                "BGM_DUCKING_RATIO": 4.0,
                "SFX_VOLUME_PRODUCT": 0.3,
                "BEFORE_AFTER_START_OFFSET": 5.0,
                "CTA_ENDCARD_ENABLED": False,
                "SUBTITLE_SAFE_ZONE_BOTTOM": 0.2,
                "FACE_ZOOM_MIN_GAP": 2.0,
                "TRANSITIONAL_HOOK_ENABLED": False,
            }
            for name, value in changes.items():
                changed_values = dict(vars(cfg))
                changed_values[name] = value
                changed = _render_fingerprint(
                    "missing.mp4",
                    SimpleNamespace(**changed_values),
                    max_clips=None,
                    cut_only=False,
                )
                self.assertNotEqual(baseline, changed, name)

            queue_only = dict(vars(cfg))
            queue_only["QUEUE_POLL_INTERVAL"] = 9.0
            self.assertEqual(
                baseline,
                _render_fingerprint(
                    "missing.mp4",
                    SimpleNamespace(**queue_only),
                    max_clips=None,
                    cut_only=False,
                ),
            )

    def test_render_fingerprint_tracks_asset_directory_contents(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bgm_dir = root / "bgm"
            bgm_dir.mkdir()
            cfg = SimpleNamespace(
                WORKING_DIR=str(root / "working"),
                OUTPUT_DIR=str(root / "output"),
                VARIANTS_PER_CLIP=1,
                OUTPUT_CODEC="h264_nvenc",
                BGM_DIR=str(bgm_dir),
                PRODUCT_BROLL_DIR="",
                FONT_HOOK_FALLBACKS=[],
            )
            first = _render_fingerprint("missing.mp4", cfg, max_clips=None, cut_only=False)
            (bgm_dir / "track.mp3").write_bytes(b"track")
            second = _render_fingerprint("missing.mp4", cfg, max_clips=None, cut_only=False)
            self.assertNotEqual(first["asset_hash"], second["asset_hash"])

    def test_completed_resume_rows_skip_failed_and_require_outputs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            raw_dir = output_dir / "raw"
            raw_dir.mkdir()
            ok_output = output_dir / "v1" / "clip_0001.mp4"
            ok_output.parent.mkdir()
            ok_output.write_bytes(b"ok")

            moments = [
                {"clip_id": "clip_0001", "start": 0, "end": 10, "score": 9, "hook": "a"},
                {"clip_id": "clip_0002", "start": 10, "end": 20, "score": 8, "hook": "b"},
                {"clip_id": "clip_0003", "start": 20, "end": 30, "score": 7, "hook": "c"},
            ]
            jobs = [_build_clip_job(moment, index, str(output_dir), raw_dir) for index, moment in enumerate(moments)]
            manifest = [
                {"clip_id": "clip_0001", "status": "ok", "output_file": "v1/clip_0001.mp4"},
                {"clip_id": "clip_0002", "status": "failed", "output_file": "clip_0002.mp4"},
                {"clip_id": "clip_0003", "status": "compliance_blocked", "output_file": "clip_0003.mp4"},
            ]

            rows = _completed_resume_rows(jobs, manifest, output_dir)

            self.assertEqual([row["clip_id"] for row in rows], ["clip_0001", "clip_0003"])

    def test_render_fingerprint_changes_when_variation_profile_revision_changes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cfg = SimpleNamespace(
                WORKING_DIR=str(root / "working"),
                OUTPUT_DIR=str(root / "output"),
                VARIANTS_PER_CLIP=4,
                OUTPUT_CODEC="h264_nvenc",
                FONT_SUBTITLE="assets/fonts/Montserrat-ExtraBold.ttf",
                FONT_HOOK="assets/fonts/Montserrat-ExtraBold.ttf",
                FONT_HOOK_FALLBACKS=[],
                SUBTITLE_FONT_DIR="assets/fonts",
                BGM_DIR=str(root / "bgm"),
                PRODUCT_BROLL_DIR=str(root / "product_broll"),
            )
            profile = default_profile(cfg)
            first = save_active_profile(cfg, profile, expected_revision=default_profile(cfg)["revision"])
            first_fp = _render_fingerprint("missing.mp4", cfg, max_clips=None, cut_only=False)

            profile["variants"][0]["highlight_color"] = "#00D4FF"
            second = save_active_profile(cfg, profile, expected_revision=first["revision"])
            second_fp = _render_fingerprint("missing.mp4", cfg, max_clips=None, cut_only=False)
            repeat_fp = _render_fingerprint("missing.mp4", cfg, max_clips=None, cut_only=False)

            self.assertNotEqual(first["revision"], second["revision"])
            self.assertNotEqual(first_fp, second_fp)
            self.assertEqual(second_fp, repeat_fp)
            self.assertEqual(
                second_fp["extra"]["variation_profile_revision"],
                second["revision"],
            )

    def test_render_fingerprint_changes_when_product_broll_assets_change_for_broll_profile(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            broll_dir = root / "product_broll" / "serum"
            broll_dir.mkdir(parents=True)
            cfg = SimpleNamespace(
                WORKING_DIR=str(root / "working"),
                OUTPUT_DIR=str(root / "output"),
                VARIANTS_PER_CLIP=1,
                OUTPUT_CODEC="h264_nvenc",
                FONT_SUBTITLE="assets/fonts/Montserrat-ExtraBold.ttf",
                FONT_HOOK="assets/fonts/Montserrat-ExtraBold.ttf",
                FONT_HOOK_FALLBACKS=[],
                SUBTITLE_FONT_DIR="assets/fonts",
                BGM_DIR=str(root / "bgm"),
                PRODUCT_BROLL_DIR=str(root / "product_broll"),
            )
            profile = default_profile(cfg)
            profile["variants"][0]["visual_mode"] = "broll_audio"
            save_active_profile(cfg, profile, expected_revision=default_profile(cfg)["revision"])

            first_fp = _render_fingerprint("missing.mp4", cfg, max_clips=None, cut_only=False)
            (broll_dir / "demo.mp4").write_bytes(b"demo")
            second_fp = _render_fingerprint("missing.mp4", cfg, max_clips=None, cut_only=False)

            self.assertNotEqual(
                first_fp["extra"]["product_broll_asset_fingerprint"],
                second_fp["extra"]["product_broll_asset_fingerprint"],
            )
            self.assertNotEqual(first_fp, second_fp)


if __name__ == "__main__":
    unittest.main()
