import tempfile
import unittest
import sys
from pathlib import Path
from unittest import mock

import config as cfg
import main


class MainModuleIntegrationTests(unittest.TestCase):
    def test_modular_assembly_default_is_opt_in(self):
        self.assertFalse(cfg.MODULE_ASSEMBLY_ENABLED)
        self.assertFalse(cfg.MODULE_VALIDATE_ON_EXTRACT)
        self.assertFalse(cfg.MODULE_PRODUCT_ZOOM_ENABLED)

    def test_extract_modules_only_returns_before_rendering(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "vod.mp4"
            video.write_bytes(b"fake")
            transcript = {
                "segments": [{"id": 0, "start": 0.0, "end": 1.0, "text": "promo serum.", "words": []}],
                "words": [{"word": "promo", "start": 0.0, "end": 0.4}],
                "metadata": {"schema_version": 3, "word_alignment_backend": "whisperx"},
            }

            with mock.patch.object(cfg, "WORKING_DIR", str(root / "working")), \
                mock.patch.object(cfg, "OUTPUT_DIR", str(root / "out")), \
                mock.patch.object(cfg, "MODULE_EXTRACTION_ENABLED", True), \
                mock.patch.object(main, "_validate_startup"), \
                mock.patch.object(main, "log"), \
                mock.patch.object(main, "_start_text_model_stage", return_value=False), \
                mock.patch.object(main, "_process_clip_job") as process_clip, \
                mock.patch("transcriber.transcribe", return_value=transcript), \
                mock.patch("transcriber.build_text_chunks", return_value=[{"chunk_start": 0.0, "chunk_end": 1.0, "text": "promo serum."}]), \
                mock.patch("moment_detector.detect_moments", return_value=[]), \
                mock.patch("module_extractor.extract_modules", return_value={"accepted": 1, "skipped_existing": 0, "rejected": 0}) as extract_modules:

                result = main.run_pipeline(str(video), extract_modules_only=True)

        extract_modules.assert_called_once()
        process_clip.assert_not_called()
        self.assertEqual(result["module_extraction"]["accepted"], 1)
        self.assertEqual(result["moments_found"], 0)

    def test_assembly_disabled_by_default_is_not_called(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "vod.mp4"
            video.write_bytes(b"fake")
            transcript = {
                "segments": [{"id": 0, "start": 0.0, "end": 1.0, "text": "promo serum.", "words": []}],
                "words": [{"word": "promo", "start": 0.0, "end": 0.4}],
                "metadata": {"schema_version": 3, "word_alignment_backend": "whisperx"},
            }

            with mock.patch.object(cfg, "WORKING_DIR", str(root / "working")), \
                mock.patch.object(cfg, "OUTPUT_DIR", str(root / "out")), \
                mock.patch.object(cfg, "MODULE_EXTRACTION_ENABLED", False), \
                mock.patch.object(cfg, "MODULE_ASSEMBLY_ENABLED", False), \
                mock.patch.object(main, "_validate_startup"), \
                mock.patch.object(main, "log"), \
                mock.patch.object(main, "_start_text_model_stage", return_value=False), \
                mock.patch.object(main, "_run_modular_assembly") as modular, \
                mock.patch("transcriber.transcribe", return_value=transcript), \
                mock.patch("transcriber.build_text_chunks", return_value=[{"chunk_start": 0.0, "chunk_end": 1.0, "text": "promo serum."}]), \
                mock.patch("moment_detector.detect_moments", return_value=[]):

                result = main.run_pipeline(str(video))

        modular.assert_not_called()
        self.assertEqual(result["moments_found"], 0)

    def test_cli_modular_flags_are_passed_to_pipeline(self):
        argv = [
            "main.py",
            "--video",
            "D:\\VOD\\sample.mp4",
            "--render-modules",
            "--modular-only",
            "--module-assembly-limit",
            "2",
            "--module-product-zoom",
        ]

        with mock.patch.object(sys, "argv", argv), mock.patch.object(main, "run_pipeline") as run_pipeline:
            main.main()

        kwargs = run_pipeline.call_args.kwargs
        self.assertTrue(kwargs["render_modules"])
        self.assertTrue(kwargs["modular_only"])
        self.assertEqual(kwargs["module_assembly_limit"], 2)
        self.assertTrue(kwargs["module_product_zoom"])

    def test_cli_visual_validation_only_does_not_require_video(self):
        argv = [
            "main.py",
            "--validate-modules-visual-only",
            "--module-visual-product",
            "serum",
            "--module-visual-status",
            "failed",
            "--module-visual-role",
            "main",
            "--module-visual-approved-only",
            "--module-visual-priority",
            "index_order",
            "--module-visual-limit",
            "5",
            "--force-module-visual",
        ]

        with mock.patch.object(sys, "argv", argv), \
            mock.patch("module_visual_validator.validate_module_library_visual", return_value={"validated": 0, "passed": 0, "failed": 0, "not_run": 0}) as validate:
            main.main()

        validate.assert_called_once()
        self.assertEqual(validate.call_args.kwargs["product"], "serum")
        self.assertEqual(validate.call_args.kwargs["limit"], 5)
        self.assertEqual(validate.call_args.kwargs["visual_status"], "failed")
        self.assertEqual(validate.call_args.kwargs["role"], "main")
        self.assertTrue(validate.call_args.kwargs["approved_only"])
        self.assertEqual(validate.call_args.kwargs["priority"], "index_order")
        self.assertTrue(validate.call_args.kwargs["force"])

    def test_cli_assemble_modules_does_not_require_video(self):
        argv = [
            "main.py",
            "--assemble-modules",
            "--date",
            "2026-04-16",
            "--module-assembly-limit",
            "2",
            "--module-product-zoom",
        ]

        with mock.patch.object(sys, "argv", argv), \
            mock.patch.object(main, "run_module_assembly", return_value={}) as assemble:
            main.main()

        assemble.assert_called_once()
        self.assertEqual(assemble.call_args.kwargs["assembly_date"], "2026-04-16")
        self.assertEqual(assemble.call_args.kwargs["module_assembly_limit"], 2)
        self.assertTrue(assemble.call_args.kwargs["module_product_zoom"])
        self.assertIsNotNone(assemble.call_args.kwargs["runtime_cfg"])

    def test_standalone_assembly_uses_dated_output_and_runtime_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seen = {}

            def fake_build(index, output_dir, runtime_cfg):
                seen["index"] = index
                seen["output_dir"] = Path(output_dir)
                seen["date_filter"] = getattr(runtime_cfg, "MODULE_ASSEMBLY_SOURCE_DATE")
                seen["limit"] = getattr(runtime_cfg, "MODULE_ASSEMBLY_RENDER_LIMIT")
                seen["zoom"] = getattr(runtime_cfg, "MODULE_PRODUCT_ZOOM_ENABLED")
                seen["subdir"] = getattr(runtime_cfg, "MODULE_ASSEMBLY_OUTPUT_SUBDIR")
                return [{"clip_id": "mod_serum_test", "output_filename": "mod_serum_test.mp4"}]

            def fake_render(jobs, runtime_cfg, output_dir=None, working_dir=None, progress_callback=None):
                seen["jobs"] = jobs
                seen["render_output_dir"] = Path(output_dir)
                seen["render_working_dir"] = Path(working_dir)
                manifest_path = Path(output_dir) / "manifest.json"
                manifest = [{"clip_id": "mod_serum_test", "status": "ok", "output_file": "mod_serum_test.mp4"}]
                return {
                    "jobs": len(jobs),
                    "created": 1,
                    "failed": 0,
                    "blocked": 0,
                    "manifest_path": str(manifest_path),
                    "manifest": manifest,
                    "scores": [],
                }

            with mock.patch.object(cfg, "OUTPUT_DIR", str(root / "out")), \
                mock.patch.object(cfg, "WORKING_DIR", str(root / "work")), \
                mock.patch.object(cfg, "MODULE_LIBRARY_DIR", str(root / "library")), \
                mock.patch.object(cfg, "COMPLIANCE_ENABLED", False), \
                mock.patch.object(cfg, "SCORER_ENABLED", False), \
                mock.patch.object(main, "_enforce_text_model_priority_at_pipeline_start"), \
                mock.patch("module_extractor.read_library_index", return_value={"module_count": 1, "modules": []}) as read_index, \
                mock.patch("module_assembler.build_modular_assembly_jobs", side_effect=fake_build), \
                mock.patch("module_assembler.render_modular_assemblies", side_effect=fake_render):

                result = main.run_module_assembly(
                    assembly_date="2026-04-16",
                    module_assembly_limit=2,
                    module_product_zoom=True,
                )

        expected_output = root / "out" / "modular_assembly" / "2026-04-16"
        expected_working = root / "work" / "modular_assembly" / "2026-04-16"
        read_index.assert_called_once()
        self.assertEqual(seen["output_dir"], expected_output)
        self.assertEqual(seen["render_output_dir"], expected_output)
        self.assertEqual(seen["render_working_dir"], expected_working)
        self.assertEqual(seen["date_filter"], "2026-04-16")
        self.assertEqual(seen["limit"], 2)
        self.assertTrue(seen["zoom"])
        self.assertEqual(seen["subdir"], "")
        self.assertEqual(result["output_dir"], str(expected_output))
        self.assertEqual(result["source_date_filter"], "2026-04-16")

    def test_modular_only_skips_normal_rendering(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "vod.mp4"
            video.write_bytes(b"fake")
            transcript = {
                "segments": [{"id": 0, "start": 0.0, "end": 1.0, "text": "promo serum.", "words": []}],
                "words": [{"word": "promo", "start": 0.0, "end": 0.4}],
                "metadata": {"schema_version": 3, "word_alignment_backend": "whisperx"},
            }
            moments = [{"clip_id": "clip_0001", "start": 0.0, "end": 10.0, "score": 9, "product": "serum"}]

            with mock.patch.object(cfg, "WORKING_DIR", str(root / "working")), \
                mock.patch.object(cfg, "OUTPUT_DIR", str(root / "out")), \
                mock.patch.object(cfg, "MODULE_EXTRACTION_ENABLED", False), \
                mock.patch.object(cfg, "COMPLIANCE_ENABLED", False), \
                mock.patch.object(cfg, "SCORER_ENABLED", False), \
                mock.patch.object(main, "_validate_startup"), \
                mock.patch.object(main, "log"), \
                mock.patch.object(main, "_start_text_model_stage", return_value=False), \
                mock.patch.object(main, "_process_clip_job") as process_clip, \
                mock.patch.object(main, "_run_modular_assembly", return_value={"created": 2, "failed": 0}) as modular, \
                mock.patch("transcriber.transcribe", return_value=transcript), \
                mock.patch("transcriber.build_text_chunks", return_value=[{"chunk_start": 0.0, "chunk_end": 1.0, "text": "promo serum."}]), \
                mock.patch("moment_detector.detect_moments", return_value=moments):

                result = main.run_pipeline(str(video), render_modules=True, modular_only=True)

        modular.assert_called_once()
        process_clip.assert_not_called()
        self.assertEqual(result["clips_created"], 2)

    def test_modular_runtime_overrides_do_not_leak_to_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "vod.mp4"
            video.write_bytes(b"fake")
            transcript = {
                "segments": [{"id": 0, "start": 0.0, "end": 1.0, "text": "promo serum.", "words": []}],
                "words": [{"word": "promo", "start": 0.0, "end": 0.4}],
                "metadata": {"schema_version": 3, "word_alignment_backend": "whisperx"},
            }
            moments = [{"clip_id": "clip_0001", "start": 0.0, "end": 10.0, "score": 9, "product": "serum"}]
            seen = {}

            def fake_modular(_output_dir, _working_dir, runtime_cfg, _progress_callback=None):
                seen["enabled"] = getattr(runtime_cfg, "MODULE_ASSEMBLY_ENABLED")
                seen["limit"] = getattr(runtime_cfg, "MODULE_ASSEMBLY_RENDER_LIMIT")
                return {"created": 0, "failed": 0}

            with mock.patch.object(cfg, "WORKING_DIR", str(root / "working")), \
                mock.patch.object(cfg, "OUTPUT_DIR", str(root / "out")), \
                mock.patch.object(cfg, "MODULE_EXTRACTION_ENABLED", False), \
                mock.patch.object(cfg, "MODULE_ASSEMBLY_ENABLED", False), \
                mock.patch.object(cfg, "MODULE_ASSEMBLY_RENDER_LIMIT", 3), \
                mock.patch.object(cfg, "COMPLIANCE_ENABLED", False), \
                mock.patch.object(cfg, "SCORER_ENABLED", False), \
                mock.patch.object(main, "_validate_startup"), \
                mock.patch.object(main, "log"), \
                mock.patch.object(main, "_start_text_model_stage", return_value=False), \
                mock.patch.object(main, "_run_modular_assembly", side_effect=fake_modular), \
                mock.patch("transcriber.transcribe", return_value=transcript), \
                mock.patch("transcriber.build_text_chunks", return_value=[{"chunk_start": 0.0, "chunk_end": 1.0, "text": "promo serum."}]), \
                mock.patch("moment_detector.detect_moments", return_value=moments):

                main.run_pipeline(str(video), render_modules=True, modular_only=True, module_assembly_limit=0)
                self.assertFalse(cfg.MODULE_ASSEMBLY_ENABLED)
                self.assertEqual(cfg.MODULE_ASSEMBLY_RENDER_LIMIT, 3)

        self.assertTrue(seen["enabled"])
        self.assertEqual(seen["limit"], 0)

    def test_normal_compliance_import_failure_fails_closed(self):
        job = {
            "clip_id": "clip_0001",
            "product": "serum",
            "output_path": "D:\\output\\clip_0001.mp4",
            "moment": {"hook": "Hook aman", "hook_overlay": {"headline": "Hook aman"}},
        }

        class Cfg:
            COMPLIANCE_ENABLED = True

        with mock.patch.dict(sys.modules, {"compliance_checker": None}):
            result = main._prepare_job_compliance(job, [], Cfg)

        self.assertTrue(result["blocked"])
        self.assertEqual(result["source"], "system_fail_closed")
        self.assertEqual(job["compliance_result"]["source"], "system_fail_closed")


if __name__ == "__main__":
    unittest.main()
