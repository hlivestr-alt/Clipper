import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import module_assembler as ma


class Cfg:
    MODULE_ASSEMBLY_RENDER_LIMIT = 3
    MODULE_ASSEMBLY_CANDIDATE_POOL = 30
    MODULE_ASSEMBLY_MAX_PER_PRODUCT = 1
    MODULE_ASSEMBLY_COMPLIANCE_PREFILTER = True
    MODULE_ASSEMBLY_SAFE_HOOKS_ENABLED = True
    MODULE_ASSEMBLY_SAME_DATE_ONLY = True
    MODULE_ASSEMBLY_REQUIRE_APPROVED = True
    MODULE_WORD_FALLBACK_REVIEW_REQUIRED = True
    MODULAR_ASSEMBLY_READY_MIN_HOOK = 1
    MODULAR_ASSEMBLY_READY_MIN_MAIN = 1
    MODULAR_ASSEMBLY_READY_MIN_CTA = 1
    MODULE_ASSEMBLY_MIN_SOURCE_VIDEOS = 1
    MODULE_PRODUCT_ZOOM_ENABLED = False
    SCORER_ENABLED = False
    COMPLIANCE_ENABLED = False


class FallbackCfg(Cfg):
    MODULE_ASSEMBLY_REQUIRE_APPROVED = False
    MODULE_WORD_FALLBACK_REVIEW_REQUIRED = False
    MODULAR_ASSEMBLY_READY_MIN_HOOK = 0
    MODULAR_ASSEMBLY_READY_MIN_MAIN = 0
    MODULAR_ASSEMBLY_READY_MIN_CTA = 0
    MODULE_ASSEMBLY_MIN_SOURCE_VIDEOS = 0


class CrossDateCfg(Cfg):
    MODULE_ASSEMBLY_SAME_DATE_ONLY = False


class DateFilterCfg(Cfg):
    MODULE_ASSEMBLY_SOURCE_DATE = "2026-04-17"


class StrictReadyCfg(Cfg):
    MODULAR_ASSEMBLY_READY_MIN_HOOK = 3
    MODULAR_ASSEMBLY_READY_MIN_MAIN = 3
    MODULAR_ASSEMBLY_READY_MIN_CTA = 3
    MODULE_ASSEMBLY_MIN_SOURCE_VIDEOS = 2


class RenderCfg(Cfg):
    MODULE_ASSEMBLY_RENDER_LIMIT = 1
    MODULE_ASSEMBLY_MAX_PER_PRODUCT = 0
    COMPLIANCE_ENABLED = True


class ZoomCfg(RenderCfg):
    MODULE_PRODUCT_ZOOM_ENABLED = True
    MODULE_ASSEMBLY_VISUAL_EVENT_BONUS = 0.75
    MODULE_ASSEMBLY_ZOOM_READY_MIN_EVENTS = 1


class ZoomRankCfg(Cfg):
    MODULE_PRODUCT_ZOOM_ENABLED = True
    MODULE_ASSEMBLY_VISUAL_EVENT_BONUS = 0.75
    MODULE_ASSEMBLY_ZOOM_READY_MIN_EVENTS = 1


class RequireZoomReadyCfg(Cfg):
    MODULE_ASSEMBLY_REQUIRE_ZOOM_READY = True
    MODULE_ASSEMBLY_ZOOM_READY_MIN_EVENTS = 1


class ProductCapCfg(RenderCfg):
    MODULE_ASSEMBLY_RENDER_LIMIT = 2
    MODULE_ASSEMBLY_MAX_PER_PRODUCT = 1


class VariantRenderCfg(Cfg):
    MODULE_ASSEMBLY_RENDER_LIMIT = 1
    MODULE_ASSEMBLY_MAX_PER_PRODUCT = 1
    VARIANTS_PER_CLIP = 2
    VARIANT_SEED = 42
    BROLL_INTRO_ENABLED = False


class ZeroLimitCfg(Cfg):
    MODULE_ASSEMBLY_RENDER_LIMIT = 0


class LockCfg(Cfg):
    MODULE_OUTPUT_LOCK_TIMEOUT = 0.1


def module_source_video(source):
    source = str(source)
    if ma.source_date_from_source_video(source):
        return source
    seconds = sum(ord(ch) for ch in source) % 60
    return rf"C:\VOD\2026-04-16-10-17-{seconds:02d}.mp4"


def module_record(
    root,
    product,
    role,
    module_id,
    duration,
    confidence=0.9,
    source="vod",
    quality_status="approved",
    boundary_mode="sentence",
):
    media = root / product / role / f"{module_id}.mp4"
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(b"video")
    source_video = module_source_video(source)
    source_date = ma.source_date_from_source_video(source_video)
    words = []
    t = 0.0
    idx = 0
    while t + 0.4 <= duration:
        token = f"kata{idx}"
        if idx % 10 == 9 or t + 1.0 >= duration:
            token = "akhir."
        words.append({"word": token, "start": round(t, 3), "end": round(t + 0.4, 3)})
        t += 0.6
        idx += 1
    record = {
        "schema_version": 2,
        "module_id": module_id,
        "product": product,
        "role": role,
        "source_video": source_video,
        "source_date": source_date,
        "source_moment_id": module_id,
        "file_path": str(media),
        "sidecar_path": str(media.with_suffix(".json")),
        "duration": duration,
        "transcript_text": f"{product} " + " ".join(word["word"] for word in words),
        "suggested_hook": "Hook PROYA",
        "confidence": confidence,
        "quality_status": quality_status,
        "quality_score": confidence * 10.0,
        "review_status": "pending",
        "boundary_mode": boundary_mode,
        "words": words,
    }
    media.with_suffix(".json").write_text(json.dumps(record), encoding="utf-8")
    return record


class ModuleAssemblerTests(unittest.TestCase):
    def test_concat_profile_requires_codecs_resolution_and_sample_rate(self):
        same = [
            {
                "video_codec": "h264",
                "audio_codec": "aac",
                "width": 1080,
                "height": 1920,
                "audio_sample_rate": 44100,
            },
            {
                "video_codec": "h264",
                "audio_codec": "aac",
                "width": 1080,
                "height": 1920,
                "audio_sample_rate": 44100,
            },
        ]
        mismatch = [same[0], {**same[1], "audio_sample_rate": 48000}]

        self.assertTrue(ma._profiles_match_for_stream_copy(same))
        self.assertFalse(ma._profiles_match_for_stream_copy(mismatch))

    def test_build_jobs_prefers_complete_same_product_assemblies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            modules = [
                module_record(root, "serum", "hook", "serum_hook_a", 6.0, source="a"),
                module_record(root, "serum", "main", "serum_main_b", 30.0, source="b"),
                module_record(root, "serum", "cta", "serum_cta_c", 7.0, source="c"),
                module_record(root, "toner", "main", "toner_main_d", 30.0, source="d"),
            ]
            index = {"modules": modules}

            jobs = ma.build_modular_assembly_jobs(index, root / "out", Cfg)

        self.assertGreaterEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["product"], "serum")
        self.assertFalse(jobs[0]["fallback_used"])
        self.assertGreaterEqual(jobs[0]["duration"], 30.0)
        self.assertLessEqual(jobs[0]["duration"], 50.0)

    def test_same_date_only_rejects_cross_date_mixes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            modules = [
                module_record(root, "serum", "hook", "serum_hook_a", 6.0, source=r"C:\VOD\2026-04-16-10-17-09.mp4"),
                module_record(root, "serum", "main", "serum_main_a", 30.0, source=r"C:\VOD\2026-04-16-10-18-09.mp4"),
                module_record(root, "serum", "cta", "serum_cta_b", 7.0, source=r"C:\VOD\2026-04-23-10-17-09.mp4"),
            ]

            with self.assertLogs("proya.module_assembler", level="WARNING") as logs:
                jobs = ma.build_modular_assembly_jobs({"modules": modules}, root / "out", Cfg)

        self.assertEqual(jobs, [])
        self.assertIn("No same-date hook+main+cta combination available for serum on 2026-04-16", "\n".join(logs.output))

    def test_same_date_only_outputs_single_source_date_components(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            modules = [
                module_record(root, "serum", "hook", "serum_hook_a", 6.0, source=r"C:\VOD\2026-04-16-10-17-09.mp4"),
                module_record(root, "serum", "main", "serum_main_a", 30.0, source=r"C:\VOD\2026-04-16-10-18-09.mp4"),
                module_record(root, "serum", "cta", "serum_cta_a", 7.0, source=r"C:\VOD\2026-04-16-10-19-09.mp4"),
                module_record(root, "serum", "cta", "serum_cta_b", 7.0, source=r"C:\VOD\2026-04-23-10-17-09.mp4"),
            ]

            jobs = ma.build_modular_assembly_jobs({"modules": modules}, root / "out", Cfg)

        self.assertGreaterEqual(len(jobs), 1)
        for job in jobs:
            source_dates = {component.get("source_date") for component in job["components"]}
            self.assertEqual(source_dates, {"2026-04-16"})
            self.assertEqual(job["source_date"], "2026-04-16")

    def test_source_date_filter_limits_assembly_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            modules = [
                module_record(root, "serum", "hook", "serum_hook_16", 6.0, source=r"C:\VOD\2026-04-16-10-17-00.mp4"),
                module_record(root, "serum", "main", "serum_main_16", 30.0, source=r"C:\VOD\2026-04-16-10-18-00.mp4"),
                module_record(root, "serum", "cta", "serum_cta_16", 7.0, source=r"C:\VOD\2026-04-16-10-19-00.mp4"),
                module_record(root, "serum", "hook", "serum_hook_17", 6.0, source=r"C:\VOD\2026-04-17-10-17-00.mp4"),
                module_record(root, "serum", "main", "serum_main_17", 30.0, source=r"C:\VOD\2026-04-17-10-18-00.mp4"),
                module_record(root, "serum", "cta", "serum_cta_17", 7.0, source=r"C:\VOD\2026-04-17-10-19-00.mp4"),
            ]

            jobs = ma.build_modular_assembly_jobs({"modules": modules}, root / "out", DateFilterCfg)

        self.assertGreaterEqual(len(jobs), 1)
        self.assertTrue(all(job["source_date"] == "2026-04-17" for job in jobs))
        for job in jobs:
            self.assertTrue(all(component.get("source_date") == "2026-04-17" for component in job["components"]))

    def test_same_date_can_be_disabled_for_cross_date_assembly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            modules = [
                module_record(root, "serum", "hook", "serum_hook_a", 6.0, source=r"C:\VOD\2026-04-16-10-17-09.mp4"),
                module_record(root, "serum", "main", "serum_main_b", 30.0, source=r"C:\VOD\2026-04-20-10-17-09.mp4"),
                module_record(root, "serum", "cta", "serum_cta_c", 7.0, source=r"C:\VOD\2026-04-23-10-17-09.mp4"),
            ]

            jobs = ma.build_modular_assembly_jobs({"modules": modules}, root / "out", CrossDateCfg)

        self.assertGreaterEqual(len(jobs), 1)
        self.assertGreater(len({component.get("source_date") for component in jobs[0]["components"]}), 1)

    def test_invalid_source_video_is_excluded_when_same_date_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hook = module_record(root, "serum", "hook", "serum_hook_bad", 6.0, source=r"C:\VOD\2026_04_16.mp4")
            hook["source_video"] = r"C:\VOD\2026_04_16.mp4"
            hook["source_date"] = ""
            Path(hook["sidecar_path"]).write_text(json.dumps(hook), encoding="utf-8")
            modules = [
                hook,
                module_record(root, "serum", "main", "serum_main_a", 30.0, source=r"C:\VOD\2026-04-16-10-18-09.mp4"),
                module_record(root, "serum", "cta", "serum_cta_a", 7.0, source=r"C:\VOD\2026-04-16-10-19-09.mp4"),
            ]

            with self.assertLogs("proya.module_assembler", level="WARNING") as logs:
                jobs = ma.build_modular_assembly_jobs({"modules": modules}, root / "out", Cfg)

        self.assertEqual(jobs, [])
        self.assertIn("has no usable source date", "\n".join(logs.output))

    def test_same_date_uses_indexed_source_date_for_nonstandard_filenames(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            modules = [
                module_record(root, "serum", "hook", "serum_hook_a", 6.0, source=r"C:\VOD\2026_04_16_hook.mp4"),
                module_record(root, "serum", "main", "serum_main_a", 30.0, source=r"C:\VOD\2026_04_16_main.mp4"),
                module_record(root, "serum", "cta", "serum_cta_a", 7.0, source=r"C:\VOD\2026_04_16_cta.mp4"),
            ]
            for module in modules:
                module["source_video"] = module["source_video"].replace("-10-17-", "_")
                module["source_date"] = "2026-04-16"
                Path(module["sidecar_path"]).write_text(json.dumps(module), encoding="utf-8")

            jobs = ma.build_modular_assembly_jobs({"modules": modules}, root / "out", Cfg)

        self.assertGreaterEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["source_date"], "2026-04-16")

    def test_same_date_builds_from_one_complete_date_after_global_readiness(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            modules = [
                module_record(root, "serum", "hook", "serum_hook_a", 6.0, source=r"C:\VOD\2026-04-16-10-17-09.mp4"),
                module_record(root, "serum", "main", "serum_main_a", 30.0, source=r"C:\VOD\2026-04-16-10-18-09.mp4"),
                module_record(root, "serum", "cta", "serum_cta_a", 7.0, source=r"C:\VOD\2026-04-16-10-19-09.mp4"),
                module_record(root, "serum", "hook", "serum_hook_b", 6.0, source=r"C:\VOD\2026-04-17-10-17-09.mp4"),
                module_record(root, "serum", "hook", "serum_hook_c", 6.0, source=r"C:\VOD\2026-04-18-10-17-09.mp4"),
                module_record(root, "serum", "main", "serum_main_b", 30.0, source=r"C:\VOD\2026-04-17-10-18-09.mp4"),
                module_record(root, "serum", "main", "serum_main_c", 30.0, source=r"C:\VOD\2026-04-18-10-18-09.mp4"),
                module_record(root, "serum", "cta", "serum_cta_b", 7.0, source=r"C:\VOD\2026-04-17-10-19-09.mp4"),
                module_record(root, "serum", "cta", "serum_cta_c", 7.0, source=r"C:\VOD\2026-04-18-10-19-09.mp4"),
            ]

            jobs = ma.build_modular_assembly_jobs({"modules": modules}, root / "out", StrictReadyCfg)

        self.assertGreaterEqual(len(jobs), 1)
        self.assertTrue(any(job["source_date"] == "2026-04-16" for job in jobs))

    def test_fallback_from_main_is_opt_in_and_avoids_duplicate_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            modules = [module_record(root, "serum", "main", "serum_main_a", 36.0, source="a")]
            index = {"modules": modules}

            jobs = ma.build_modular_assembly_jobs(index, root / "out", FallbackCfg)

        self.assertEqual(len(jobs), 1)
        self.assertTrue(jobs[0]["fallback_used"])
        fallback_roles = [component["role"] for component in jobs[0]["components"] if component["fallback"]]
        self.assertEqual(fallback_roles, ["hook", "cta"])
        self.assertGreater(jobs[0]["components"][1]["duration"], 5.0)

    def test_needs_review_modules_are_excluded_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            modules = [
                module_record(root, "serum", "hook", "serum_hook_a", 6.0, source="a", quality_status="needs_review", boundary_mode="word_boundary_fallback"),
                module_record(root, "serum", "main", "serum_main_b", 30.0, source="b"),
                module_record(root, "serum", "cta", "serum_cta_c", 7.0, source="c"),
            ]
            index = {"modules": modules}

            jobs = ma.build_modular_assembly_jobs(index, root / "out", Cfg)

        self.assertEqual(jobs, [])

    def test_no_visual_events_modules_remain_assembly_eligible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            modules = [
                module_record(root, "serum", "hook", "serum_hook_a", 6.0, source="a", quality_status="no_visual_events"),
                module_record(root, "serum", "main", "serum_main_b", 30.0, source="b", quality_status="no_visual_events"),
                module_record(root, "serum", "cta", "serum_cta_c", 7.0, source="c", quality_status="no_visual_events"),
            ]
            for module in modules:
                module["visual_validation_status"] = "failed"
                module["visual_validation_reason"] = "source_vod_no_visual_events"
                Path(module["sidecar_path"]).write_text(json.dumps(module), encoding="utf-8")
            index = {"modules": modules}

            jobs = ma.build_modular_assembly_jobs(index, root / "out", Cfg)

        self.assertGreaterEqual(len(jobs), 1)
        self.assertTrue(all(component["quality_status"] == "no_visual_events" for component in jobs[0]["components"]))

    def test_component_slice_reencodes_for_frame_accurate_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.mp4"
            target = root / "slice.mp4"
            source.write_bytes(b"video")

            calls = []

            def fake_run(cmd, **_kwargs):
                calls.append(cmd)
                target.write_bytes(b"slice")
                return type("Result", (), {"returncode": 0, "stderr": ""})()

            original = ma.subprocess.run
            ma.subprocess.run = fake_run
            try:
                ma._cut_component_slice(source, target, 1.0, 5.0)
            finally:
                ma.subprocess.run = original

        cmd = calls[0]
        self.assertIn("libx264", cmd)
        self.assertIn("aac", cmd)
        self.assertNotIn("copy", cmd)

    def test_safe_hook_replaces_risky_default_phrase(self):
        hook = ma._hook_text_for_components(
            "cleanser",
            [{"suggested_hook": "Rekomendasi Cleanser Terbaik"}],
            "cleanser ini cocok untuk kulit terasa ketarik",
            Cfg,
        )

        self.assertNotIn("Terbaik", hook)
        self.assertEqual(hook, "Wajah Ketarik Pas Cuci Muka?")

    def test_safe_hook_replaces_risky_result_claim(self):
        hook = ma._hook_text_for_components(
            "serum",
            [{"suggested_hook": "Serum Mencerahkan & Hilangkan Flek Hitam"}],
            "serum untuk kulit tampak cerah",
            Cfg,
        )

        self.assertEqual(hook, "Kulit Kusam? Cek Step Ini")

    def test_render_limit_zero_creates_no_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job = ma._build_job(
                "serum",
                [
                    ma._component_from_module(module_record(root, "serum", "hook", "serum_hook_a", 6.0, source="a")),
                    ma._component_from_module(module_record(root, "serum", "main", "serum_main_b", 30.0, source="b")),
                    ma._component_from_module(module_record(root, "serum", "cta", "serum_cta_c", 7.0, source="c")),
                ],
                root / "out",
                fallback_used=False,
                cfg=Cfg,
            )

            result = ma.render_modular_assemblies([job], ZeroLimitCfg, output_dir=root / "out", working_dir=root / "work")

        self.assertEqual(result["created"], 0)
        self.assertEqual(result["pool_examined"], 0)
        self.assertEqual(result["manifest"], [])

    def test_existing_invalid_output_is_not_trusted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job = ma._build_job(
                "serum",
                [
                    ma._component_from_module(module_record(root, "serum", "hook", "serum_hook_a", 6.0, source="a")),
                    ma._component_from_module(module_record(root, "serum", "main", "serum_main_b", 30.0, source="b")),
                    ma._component_from_module(module_record(root, "serum", "cta", "serum_cta_c", 7.0, source="c")),
                ],
                root / "out",
                fallback_used=False,
                cfg=Cfg,
            )
            output = root / "out" / "modular" / job["output_filename"]
            output.parent.mkdir(parents=True)
            output.write_bytes(b"not a valid mp4")

            def fake_edit(**kwargs):
                Path(kwargs["output_path"]).write_bytes(b"video")
                return True

            original_build = ma._build_raw_assembly
            try:
                ma._build_raw_assembly = lambda job, raw_root, cfg: Path(job["raw_path"]).write_bytes(b"raw")
                import ffmpeg_editor

                original_edit = ffmpeg_editor.edit_clip
                ffmpeg_editor.edit_clip = fake_edit
                try:
                    result = ma.render_modular_assemblies([job], Cfg, output_dir=root / "out", working_dir=root / "work")
                finally:
                    ffmpeg_editor.edit_clip = original_edit
            finally:
                ma._build_raw_assembly = original_build

        self.assertEqual(result["created"], 1)
        self.assertEqual(result["skipped"], 0)

    def test_existing_valid_output_still_runs_compliance_prefilter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job = ma._build_job(
                "serum",
                [
                    ma._component_from_module(module_record(root, "serum", "hook", "serum_hook_a", 6.0, source="a")),
                    ma._component_from_module(module_record(root, "serum", "main", "serum_main_b", 30.0, source="b")),
                    ma._component_from_module(module_record(root, "serum", "cta", "serum_cta_c", 7.0, source="c")),
                ],
                root / "out",
                fallback_used=False,
                cfg=Cfg,
            )
            output = root / "out" / "modular" / job["output_filename"]
            output.parent.mkdir(parents=True)
            output.write_bytes(b"valid enough for mocked probe")

            def fake_compliance(job, _cfg):
                result = {
                    "passed": False,
                    "blocked": True,
                    "violation_count": 1,
                    "violations": [{"severity": "high", "violation_type": "absolute_claim", "original_text": "nomor 1"}],
                    "compliance_summary": "blocked",
                }
                job["compliance_result"] = result
                return result

            original_probe = ma.probe_media
            original_build = ma._build_raw_assembly
            original_compliance = ma._apply_compliance
            try:
                ma.probe_media = lambda path: {"duration": job["duration"], "has_video": True, "has_audio": True}
                ma._build_raw_assembly = mock.Mock()
                ma._apply_compliance = fake_compliance
                result = ma.render_modular_assemblies([job], RenderCfg, output_dir=root / "out", working_dir=root / "work")
                ma._build_raw_assembly.assert_not_called()
            finally:
                ma.probe_media = original_probe
                ma._build_raw_assembly = original_build
                ma._apply_compliance = original_compliance

        self.assertEqual(result["created"], 0)
        self.assertEqual(result["blocked"], 1)
        self.assertEqual(result["manifest"][0]["status"], "compliance_blocked")

    def test_compliance_unavailable_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job = ma._build_job(
                "serum",
                [
                    ma._component_from_module(module_record(root, "serum", "hook", "serum_hook_a", 6.0, source="a")),
                    ma._component_from_module(module_record(root, "serum", "main", "serum_main_b", 30.0, source="b")),
                    ma._component_from_module(module_record(root, "serum", "cta", "serum_cta_c", 7.0, source="c")),
                ],
                root / "out",
                fallback_used=False,
                cfg=Cfg,
            )
            job["output_path"] = str(root / "out" / "modular" / job["output_filename"])

            with mock.patch.dict(sys.modules, {"compliance_checker": None}):
                result = ma._apply_compliance(job, RenderCfg)

        self.assertTrue(result["blocked"])
        self.assertEqual(result["source"], "system_fail_closed")

    def test_modular_output_lock_times_out_when_already_held(self):
        with tempfile.TemporaryDirectory() as tmp:
            modular_dir = Path(tmp) / "modular"
            with ma.modular_output_lock(modular_dir, LockCfg):
                with self.assertRaises(RuntimeError):
                    with ma.modular_output_lock(modular_dir, LockCfg):
                        pass

    def test_assembly_digest_includes_slice_end_and_fallback_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            main = module_record(root, "serum", "main", "serum_main_a", 36.0, source="a")
            base_components = [
                ma._component_from_module(module_record(root, "serum", "hook", "serum_hook_a", 6.0, source="b")),
                ma._component_from_module(main, role="main", slice_start=6.0, slice_end=20.0),
                ma._component_from_module(module_record(root, "serum", "cta", "serum_cta_a", 7.0, source="c")),
            ]
            changed_components = [dict(component) for component in base_components]
            changed_components[1]["slice_end"] = 21.0
            changed_components[1]["duration"] = 15.0
            changed_components[1]["words"] = ma._slice_words(main["words"], 6.0, 21.0)

            first = ma._build_job("serum", base_components, root / "out", fallback_used=False, cfg=Cfg)
            second = ma._build_job("serum", changed_components, root / "out", fallback_used=False, cfg=Cfg)

        self.assertNotEqual(first["clip_id"], second["clip_id"])

    def test_component_source_key_falls_back_to_module_id(self):
        component = {
            "source_video": "vod.mp4",
            "source_moment_id": "",
            "module_id": "serum_main_a",
        }

        self.assertIn("serum_main_a", ma._component_source_key(component))

    def test_modules_without_words_are_not_ready_for_assembly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            modules = [
                module_record(root, "serum", "hook", "serum_hook_a", 6.0, source="a"),
                module_record(root, "serum", "main", "serum_main_b", 30.0, source="b"),
                module_record(root, "serum", "cta", "serum_cta_c", 7.0, source="c"),
            ]
            for module in modules:
                module["words"] = []
                Path(module["sidecar_path"]).write_text(json.dumps(module), encoding="utf-8")

            jobs = ma.build_modular_assembly_jobs({"modules": modules}, root / "out", Cfg)

        self.assertEqual(jobs, [])

    def test_assembly_rank_prefers_visual_passed_modules(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            passed = ma._component_from_module(module_record(root, "serum", "hook", "serum_hook_passed", 6.0))
            failed = ma._component_from_module(module_record(root, "serum", "hook", "serum_hook_failed", 6.0))
            passed["visual_validation_status"] = "passed"
            failed["visual_validation_status"] = "failed"

            base_main = ma._component_from_module(module_record(root, "serum", "main", "serum_main_a", 30.0))
            base_cta = ma._component_from_module(module_record(root, "serum", "cta", "serum_cta_a", 7.0))

            passed_score = ma._rank_components([passed, base_main, base_cta], 43.0)
            failed_score = ma._rank_components([failed, base_main, base_cta], 43.0)

        self.assertGreater(passed_score, failed_score)

    def test_product_zoom_disabled_keeps_empty_product_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hook = module_record(root, "serum", "hook", "serum_hook_a", 6.0)
            hook["visual_validation_status"] = "passed"
            hook["visual_product_events"] = [
                {
                    "product": "serum",
                    "class_name": "serum",
                    "relative_start": 1.0,
                    "relative_end": 2.0,
                    "best_bbox": [10, 10, 100, 100],
                    "frame_w": 1080,
                    "frame_h": 1920,
                    "relative_track": [{"relative_time": 1.2, "bbox": [10, 10, 100, 100], "confidence": 0.9}],
                }
            ]
            job = ma._build_job(
                "serum",
                [
                    ma._component_from_module(hook),
                    ma._component_from_module(module_record(root, "serum", "main", "serum_main_b", 30.0)),
                    ma._component_from_module(module_record(root, "serum", "cta", "serum_cta_c", 7.0)),
                ],
                root / "out",
                fallback_used=False,
                cfg=Cfg,
            )
            captured = {}

            def fake_edit(**kwargs):
                captured["product_events"] = kwargs["product_events"]
                Path(kwargs["output_path"]).write_bytes(b"video")
                return True

            original_build = ma._build_raw_assembly
            try:
                ma._build_raw_assembly = lambda job, raw_root, cfg: Path(job["raw_path"]).write_bytes(b"raw")
                import ffmpeg_editor

                original_edit = ffmpeg_editor.edit_clip
                ffmpeg_editor.edit_clip = fake_edit
                try:
                    result = ma.render_modular_assemblies([job], Cfg, output_dir=root / "out", working_dir=root / "work")
                finally:
                    ffmpeg_editor.edit_clip = original_edit
            finally:
                ma._build_raw_assembly = original_build

        self.assertEqual(captured["product_events"], [])
        self.assertFalse(result["manifest"][0]["module_product_zoom_enabled"])
        self.assertEqual(result["manifest"][0]["product_events"], 0)
        self.assertTrue(result["manifest"][0]["zoom_ready"])
        self.assertEqual(result["manifest"][0]["visual_product_event_count_available"], 1)
        self.assertEqual(result["manifest"][0]["visual_product_event_count"], 0)

    def test_product_zoom_enabled_remaps_validated_same_product_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hook = module_record(root, "serum", "hook", "serum_hook_a", 6.0)
            hook["visual_validation_status"] = "passed"
            hook["visual_product_events"] = [
                {
                    "product": "serum",
                    "class_name": "serum",
                    "relative_start": 1.0,
                    "relative_end": 2.0,
                    "best_bbox": [10, 10, 100, 100],
                    "frame_w": 1080,
                    "frame_h": 1920,
                    "relative_track": [{"relative_time": 1.2, "bbox": [10, 10, 100, 100], "confidence": 0.9}],
                },
                {
                    "product": "toner",
                    "class_name": "toner",
                    "relative_start": 2.5,
                    "relative_end": 3.0,
                    "best_bbox": [20, 20, 120, 120],
                    "frame_w": 1080,
                    "frame_h": 1920,
                },
            ]
            main = module_record(root, "serum", "main", "serum_main_b", 30.0)
            main["visual_validation_status"] = "failed"
            main["visual_product_events"] = [
                {
                    "product": "serum",
                    "class_name": "serum",
                    "relative_start": 3.0,
                    "relative_end": 4.0,
                    "best_bbox": [30, 30, 130, 130],
                    "frame_w": 1080,
                    "frame_h": 1920,
                }
            ]
            job = ma._build_job(
                "serum",
                [
                    ma._component_from_module(hook),
                    ma._component_from_module(main),
                    ma._component_from_module(module_record(root, "serum", "cta", "serum_cta_c", 7.0)),
                ],
                root / "out",
                fallback_used=False,
                cfg=ZoomCfg,
            )
            captured = {}

            def fake_edit(**kwargs):
                captured["product_events"] = kwargs["product_events"]
                Path(kwargs["output_path"]).write_bytes(b"video")
                return True

            original_build = ma._build_raw_assembly
            try:
                ma._build_raw_assembly = lambda job, raw_root, cfg: Path(job["raw_path"]).write_bytes(b"raw")
                import ffmpeg_editor

                original_edit = ffmpeg_editor.edit_clip
                ffmpeg_editor.edit_clip = fake_edit
                try:
                    result = ma.render_modular_assemblies([job], ZoomCfg, output_dir=root / "out", working_dir=root / "work")
                finally:
                    ffmpeg_editor.edit_clip = original_edit
            finally:
                ma._build_raw_assembly = original_build

        self.assertEqual(len(captured["product_events"]), 1)
        self.assertEqual(captured["product_events"][0]["relative_start"], 1.0)
        self.assertTrue(result["manifest"][0]["module_product_zoom_enabled"])
        self.assertTrue(result["manifest"][0]["zoom_ready"])
        self.assertEqual(result["manifest"][0]["visual_product_event_count_available"], 1)
        self.assertEqual(result["manifest"][0]["visual_product_event_count"], 1)
        self.assertEqual(len(result["manifest"][0]["visual_product_events"]), 1)

    def test_product_zoom_enabled_prefers_zoom_ready_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            no_event_hook = module_record(root, "serum", "hook", "serum_hook_no_event", 6.0, confidence=0.9)
            no_event_hook["visual_validation_status"] = "passed"
            no_event_hook["visual_product_events"] = []
            Path(no_event_hook["sidecar_path"]).write_text(json.dumps(no_event_hook), encoding="utf-8")
            event_hook = module_record(root, "serum", "hook", "serum_hook_event", 6.0, confidence=0.9)
            event_hook["visual_validation_status"] = "passed"
            event_hook["visual_product_events"] = [
                {
                    "product": "serum",
                    "class_name": "serum",
                    "relative_start": 1.0,
                    "relative_end": 2.0,
                    "best_bbox": [10, 10, 100, 100],
                    "frame_w": 1080,
                    "frame_h": 1920,
                }
            ]
            Path(event_hook["sidecar_path"]).write_text(json.dumps(event_hook), encoding="utf-8")
            modules = [
                no_event_hook,
                event_hook,
                module_record(root, "serum", "main", "serum_main_a", 30.0, confidence=0.9),
                module_record(root, "serum", "cta", "serum_cta_a", 7.0, confidence=0.9),
            ]

            jobs = ma.build_modular_assembly_jobs({"modules": modules}, root / "out", ZoomRankCfg)

        self.assertGreaterEqual(len(jobs), 2)
        self.assertIn("serum_hook_event", jobs[0]["source_module_ids"])
        self.assertTrue(jobs[0]["zoom_ready"])
        self.assertEqual(jobs[0]["visual_product_event_count_available"], 1)

    def test_require_zoom_ready_excludes_non_ready_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            modules = [
                module_record(root, "serum", "hook", "serum_hook_a", 6.0, source="a"),
                module_record(root, "serum", "main", "serum_main_b", 30.0, source="b"),
                module_record(root, "serum", "cta", "serum_cta_c", 7.0, source="c"),
            ]

            jobs = ma.build_modular_assembly_jobs({"modules": modules}, root / "out", RequireZoomReadyCfg)

        self.assertEqual(jobs, [])

    def test_fallback_slice_remaps_visual_event_timing(self):
        event = {
            "product": "serum",
            "class_name": "serum",
            "relative_start": 1.0,
            "relative_end": 6.0,
            "best_bbox": [10, 10, 100, 100],
            "frame_w": 1080,
            "frame_h": 1920,
            "relative_track": [
                {"relative_time": 1.5, "bbox": [10, 10, 100, 100], "confidence": 0.8},
                {"relative_time": 4.0, "bbox": [20, 20, 120, 120], "confidence": 0.9},
            ],
        }
        component = {
            "role": "hook",
            "duration": 4.0,
            "slice_start": 2.0,
            "slice_end": 6.0,
            "visual_validation_status": "passed",
            "visual_product_events": [event],
        }
        job = {"product": "serum", "components": [component]}

        events = ma._assembly_visual_product_events(job, ZoomCfg)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["relative_start"], 0.0)
        self.assertEqual(events[0]["relative_end"], 4.0)
        self.assertEqual(len(events[0]["relative_track"]), 1)
        self.assertEqual(events[0]["relative_track"][0]["relative_time"], 2.0)

    def test_render_continues_after_compliance_blocked_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            modules = [
                module_record(root, "serum", "hook", "serum_hook_a", 6.0, source="a"),
                module_record(root, "serum", "main", "serum_main_b", 30.0, source="b"),
                module_record(root, "serum", "cta", "serum_cta_c", 7.0, source="c"),
                module_record(root, "toner", "hook", "toner_hook_a", 6.0, source="a"),
                module_record(root, "toner", "main", "toner_main_b", 30.0, source="b"),
                module_record(root, "toner", "cta", "toner_cta_c", 7.0, source="c"),
            ]
            jobs = ma.build_modular_assembly_jobs({"modules": modules}, root / "out", Cfg)[:2]

            def fake_compliance(job, _cfg):
                blocked = job["product"] == jobs[0]["product"]
                result = {
                    "passed": not blocked,
                    "blocked": blocked,
                    "violation_count": 1 if blocked else 0,
                    "violations": [{"severity": "high", "violation_type": "absolute_claim", "original_text": "terbaik"}] if blocked else [],
                    "compliance_summary": "blocked" if blocked else "ok",
                }
                job["compliance_result"] = result
                return result

            def fake_edit(**kwargs):
                Path(kwargs["output_path"]).write_bytes(b"video")
                return True

            original_build = ma._build_raw_assembly
            original_compliance = ma._apply_compliance
            try:
                ma._build_raw_assembly = lambda job, raw_root, cfg: Path(job["raw_path"]).write_bytes(b"raw")
                ma._apply_compliance = fake_compliance
                import ffmpeg_editor

                original_edit = ffmpeg_editor.edit_clip
                ffmpeg_editor.edit_clip = fake_edit
                try:
                    result = ma.render_modular_assemblies(jobs, RenderCfg, output_dir=root / "out", working_dir=root / "work")
                finally:
                    ffmpeg_editor.edit_clip = original_edit
            finally:
                ma._build_raw_assembly = original_build
                ma._apply_compliance = original_compliance

        self.assertEqual(result["created"], 1)
        self.assertEqual(result["blocked"], 1)
        self.assertEqual(result["pool_examined"], 2)
        self.assertEqual(result["manifest"][0]["status"], "compliance_blocked")
        self.assertIn("blocked_reason", result["manifest"][0])

    def test_product_cap_limits_repeated_products(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = ma._build_job(
                "serum",
                [
                    ma._component_from_module(module_record(root, "serum", "hook", "serum_hook_a", 6.0, source="a")),
                    ma._component_from_module(module_record(root, "serum", "main", "serum_main_b", 30.0, source="b")),
                    ma._component_from_module(module_record(root, "serum", "cta", "serum_cta_c", 7.0, source="c")),
                ],
                root / "out",
                fallback_used=False,
                cfg=Cfg,
            )
            second_same_product = dict(first)
            second_same_product["clip_id"] = "mod_serum_second"
            second_same_product["output_filename"] = "mod_serum_second.mp4"
            third = ma._build_job(
                "toner",
                [
                    ma._component_from_module(module_record(root, "toner", "hook", "toner_hook_a", 6.0, source="a")),
                    ma._component_from_module(module_record(root, "toner", "main", "toner_main_b", 30.0, source="b")),
                    ma._component_from_module(module_record(root, "toner", "cta", "toner_cta_c", 7.0, source="c")),
                ],
                root / "out",
                fallback_used=False,
                cfg=Cfg,
            )

            def fake_edit(**kwargs):
                Path(kwargs["output_path"]).write_bytes(b"video")
                return True

            original_build = ma._build_raw_assembly
            original_compliance = ma._apply_compliance
            try:
                ma._build_raw_assembly = lambda job, raw_root, cfg: Path(job["raw_path"]).write_bytes(b"raw")
                ma._apply_compliance = lambda job, cfg: {"passed": True, "blocked": False, "violation_count": 0, "violations": [], "compliance_summary": "ok"}
                import ffmpeg_editor

                original_edit = ffmpeg_editor.edit_clip
                ffmpeg_editor.edit_clip = fake_edit
                try:
                    result = ma.render_modular_assemblies([first, second_same_product, third], ProductCapCfg, output_dir=root / "out", working_dir=root / "work")
                finally:
                    ffmpeg_editor.edit_clip = original_edit
            finally:
                ma._build_raw_assembly = original_build
                ma._apply_compliance = original_compliance

        self.assertEqual(result["created"], 2)
        self.assertEqual(result["products_created"], 2)
        self.assertEqual([row["product"] for row in result["manifest"]], ["serum", "toner"])

    def test_render_expands_modular_variants_like_normal_clips(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job = ma._build_job(
                "serum",
                [
                    ma._component_from_module(module_record(root, "serum", "hook", "serum_hook_a", 6.0, source="a")),
                    ma._component_from_module(module_record(root, "serum", "main", "serum_main_b", 30.0, source="b")),
                    ma._component_from_module(module_record(root, "serum", "cta", "serum_cta_c", 7.0, source="c")),
                ],
                root / "out",
                fallback_used=False,
                cfg=VariantRenderCfg,
            )
            captured_variant_ids = []

            def fake_edit(**kwargs):
                Path(kwargs["output_path"]).parent.mkdir(parents=True, exist_ok=True)
                Path(kwargs["output_path"]).write_bytes(b"video")
                captured_variant_ids.append(getattr(kwargs["cfg"], "_variant_id", ""))
                return True

            original_build = ma._build_raw_assembly
            try:
                ma._build_raw_assembly = lambda job, raw_root, cfg: Path(job["raw_path"]).write_bytes(b"raw")
                import ffmpeg_editor

                original_edit = ffmpeg_editor.edit_clip
                ffmpeg_editor.edit_clip = fake_edit
                try:
                    result = ma.render_modular_assemblies([job], VariantRenderCfg, output_dir=root / "out", working_dir=root / "work")
                finally:
                    ffmpeg_editor.edit_clip = original_edit
            finally:
                ma._build_raw_assembly = original_build

        self.assertEqual(result["jobs"], 1)
        self.assertEqual(result["render_jobs"], 2)
        self.assertEqual(result["created"], 2)
        self.assertEqual([row["version_dir"] for row in result["manifest"]], ["v0", "v1"])
        self.assertEqual({row["base_clip_id"] for row in result["manifest"]}, {job["clip_id"]})
        self.assertTrue(all(row["output_file"].startswith("modular/v") for row in result["manifest"]))
        self.assertIn("v0_original", captured_variant_ids)
        self.assertEqual(len(captured_variant_ids), 2)


if __name__ == "__main__":
    unittest.main()
