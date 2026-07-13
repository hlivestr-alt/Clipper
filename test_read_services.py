import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from clipper_app.application.read_services import ReadDashboardService
from clipper_app.application.settings import LegacyConfigProvider


class ReadServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.output_root = self.root / "output"
        self.working = self.root / "working"
        self.module_library = self.root / "modules"
        self.output_root.mkdir()
        self.working.mkdir()
        self.module_library.mkdir()
        self.run_dir = self.output_root / "2026-06-01-10-00-00__run_001"
        self.run_dir.mkdir()
        (self.run_dir / "clip_001.mp4").write_bytes(b"fake media")
        (self.working / "pipeline.log").write_text("one\ntwo\nthree\n", encoding="utf-8")

        self.state_path = self.working / "video_queue_state.json"
        self.state_path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "queue_status": "completed",
                    "updated_at": "2026-06-01T11:00:00+08:00",
                    "videos": {
                        "vod": {
                            "name": "2026-06-01-10-00-00.mp4",
                            "path": "D:/VOD/2026-06-01-10-00-00.mp4",
                            "status": "completed",
                            "created_at": "2026-06-01T10:00:00+08:00",
                            "completed_at": "2026-06-01T11:00:00+08:00",
                            "output_dir": str(self.run_dir),
                            "working_dir": str(self.working / "run_001"),
                            "stages": {
                                "transcribe": {"status": "done", "finished_at": "2026-06-01T10:10:00+08:00"},
                                "llm": {"status": "done", "finished_at": "2026-06-01T10:20:00+08:00"},
                                "yolo": {"status": "done", "finished_at": "2026-06-01T10:30:00+08:00"},
                                "ffmpeg": {
                                    "status": "done",
                                    "clips_created": 1,
                                    "finished_at": "2026-06-01T11:00:00+08:00",
                                },
                            },
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        (self.run_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "clips": [
                        {
                            "clip_id": "clip_001",
                            "product": "serum",
                            "status": "completed",
                            "output_file": "clip_001.mp4",
                            "compliance_passed": False,
                            "compliance_blocked": True,
                            "violation_count": 1,
                            "compliance_file": "clip_001_compliance.json",
                            "compliance_checked_at": "2026-06-01T11:05:00+08:00",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        (self.run_dir / "clip_001_compliance.json").write_text(
            json.dumps(
                {
                    "checked_at": "2026-06-01T11:05:00+08:00",
                    "passed": False,
                    "blocked": True,
                    "violation_count": 1,
                    "violations": [
                        {
                            "source_field": "transcript",
                            "severity": "high",
                            "violation_type": "claim",
                            "original_text": "instant whitening",
                            "suggested_replacement": "bright-looking skin",
                            "position": {"start": 1, "end": 10},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        (self.run_dir / "scores_summary.json").write_text(
            json.dumps(
                {
                    "groups": [
                        {
                            "clip_id": "clip_001",
                            "base_clip_id": "clip_001",
                            "product": "serum",
                            "total_score": 8.2,
                            "quality_score": 7.5,
                            "content_score": 8.0,
                            "engagement_score": 8.1,
                            "representative_output_file": "clip_001.mp4",
                            "summary": "Strong selling moment",
                            "scored_at": "2026-06-01T11:04:00+08:00",
                            "variants": [
                                {
                                    "clip_id": "clip_001_v1",
                                    "variant_index": 1,
                                    "output_file": "clip_001.mp4",
                                    "similarity_score": 0.45,
                                }
                            ],
                        }
                    ],
                    "scoring_optimization": {"actual_text_qwen_calls": 1},
                }
            ),
            encoding="utf-8",
        )
        (self.module_library / "module_001.mp4").write_bytes(b"fake module")
        (self.module_library / "index.json").write_text(
            json.dumps(
                {
                    "updated_at": "2026-06-01T12:00:00+08:00",
                    "module_count": 1,
                    "modules": [
                        {
                            "module_id": "module_001",
                            "product": "serum",
                            "role": "hook",
                            "source_video": "2026-06-01-10-00-00.mp4",
                            "duration": 5.2,
                            "confidence": 0.9,
                            "quality_status": "approved",
                            "review_status": "needs_review",
                            "visual_validation_status": "passed",
                            "visual_product_hits": 1,
                            "file_path": str(self.module_library / "module_001.mp4"),
                            "transcript_text": "serum intro",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.config = SimpleNamespace(
            OUTPUT_DIR=str(self.output_root),
            WORKING_DIR=str(self.working),
            QUEUE_STATE_FILE=str(self.state_path),
            QUEUE_CONTROL_FILE=str(self.working / "queue_control.json"),
            QUEUE_FOREVER_STATE_FILE=str(self.working / "queue_forever_state.json"),
            MODULE_LIBRARY_DIR=str(self.module_library),
            QUEUE_DASHBOARD_RUNNING_STALL_SECONDS=7200.0,
            QUEUE_DASHBOARD_QUEUED_STALL_SECONDS=86400.0,
            QUEUE_STAGE_ADMISSION_LIMIT=3,
            MODULAR_ASSEMBLY_READY_MIN_HOOK=1,
            MODULAR_ASSEMBLY_READY_MIN_MAIN=1,
            MODULAR_ASSEMBLY_READY_MIN_CTA=1,
            MODULE_ASSEMBLY_ZOOM_READY_MIN_EVENTS=1,
            MIN_SCORE=7.0,
            MAX_PARALLEL_CLIPS=2,
            READ_APP_MAX_OUTPUT_DIRS=200,
        )
        self.service = ReadDashboardService(LegacyConfigProvider(self.config))

    def tearDown(self):
        self.temp.cleanup()

    def test_dashboard_and_queue_are_typed_and_read_only(self):
        result = self.service.dashboard()
        self.assertEqual(result.data.total_videos, 1)
        self.assertEqual(result.data.total_clips, 1)
        self.assertEqual(result.data.rows[0].status, "Completed")
        self.assertTrue(result.data.rows[0].run_id)
        self.assertEqual(len(result.data.production_days), 7)
        self.assertGreaterEqual(result.data.clips_today, 0)
        self.assertEqual(Path(result.source_signatures[0].path).resolve(), self.state_path.resolve())

    def test_queue_detail_uses_completed_one_shot_supervisor_over_stale_control(self):
        Path(self.config.QUEUE_CONTROL_FILE).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "requested_action": "run",
                    "status": "running",
                    "current_run_tag": "_run_146",
                    "launch_config": {
                        "run_mode": "single_video",
                        "pipeline_mode": "full",
                        "variant_mode": "all",
                        "variant_count": 1,
                        "max_clips": 2,
                        "video_path": "D:/VOD/2026-06-01-10-00-00.mp4",
                    },
                }
            ),
            encoding="utf-8",
        )
        Path(self.config.QUEUE_FOREVER_STATE_FILE).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "current_run_tag": "_run_146",
                    "status": "completed",
                    "queue_summary": {"is_terminal": True},
                }
            ),
            encoding="utf-8",
        )

        result = self.service.queue_detail()

        self.assertEqual(result.data.control_status, "completed")
        self.assertEqual(result.data.launch_config["run_mode"], "single_video")

    def test_queue_detail_reports_waiting_admission_counts(self):
        waiting_video = self.root / "vod_waiting.mp4"
        queued_video = self.root / "vod_queued.mp4"
        waiting_video.write_bytes(b"video")
        queued_video.write_bytes(b"video")
        self.state_path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "queue_status": "idle",
                    "active_stages": ["ffmpeg"],
                    "stage_admission_limit": 3,
                    "videos": {
                        str(waiting_video.resolve()): {
                            "name": waiting_video.name,
                            "path": str(waiting_video.resolve()),
                            "status": "waiting",
                            "stages": {
                                "ffmpeg": {"status": "pending", "queued": False},
                            },
                        },
                        str(queued_video.resolve()): {
                            "name": queued_video.name,
                            "path": str(queued_video.resolve()),
                            "status": "queued",
                            "stages": {
                                "ffmpeg": {"status": "queued", "queued": True},
                            },
                        },
                    },
                }
            ),
            encoding="utf-8",
        )

        result = self.service.queue_detail()
        dashboard = self.service.dashboard()

        self.assertEqual(result.data.waiting_videos, 1)
        self.assertEqual(result.data.stage_waiting["ffmpeg"], 1)
        self.assertEqual(result.data.stage_admission_limit, 3)
        self.assertEqual(dashboard.data.waiting_videos, 1)
        self.assertEqual(dashboard.data.stage_waiting["ffmpeg"], 1)

    def test_scores_index_and_detail_use_score_summary(self):
        result = self.service.scores(limit=10)
        self.assertEqual(result.data.total, 2)
        self.assertEqual(result.data.rows[0].status, "Strong")
        self.assertEqual(result.data.stats.actual_text_qwen_calls, 1)
        self.assertIn("serum", result.data.filter_options["product"])
        self.assertIn("Strong", result.data.filter_options["status"])

        detail = self.service.score_detail(result.data.rows[0].score_key)
        self.assertIsNotNone(detail.data.selected)
        self.assertEqual(len(detail.data.variants), 2)

    def test_scores_include_fresh_output_dir_when_old_history_exceeds_limit(self):
        old_dirs = []
        for index in range(3):
            old_dir = self.output_root / f"2026-05-15-10-52-43__run_{index + 1:03d}"
            old_dir.mkdir()
            (old_dir / "scores_summary.json").write_text(
                json.dumps(
                    {
                        "groups": [
                            {
                                "clip_id": f"clip_old_{index}",
                                "base_clip_id": f"clip_old_{index}",
                                "representative_output_file": f"clip_old_{index}.mp4",
                                "scored_at": f"2026-06-2{index}T06:18:00+08:00",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            os.utime(old_dir, (1_800_000_000 + index, 1_800_000_000 + index))
            old_dirs.append(old_dir)

        fresh_dir = self.output_root / "2026-05-15-10-52-43__run_173"
        fresh_dir.mkdir()
        (fresh_dir / "scores_summary.json").write_text(
            json.dumps(
                {
                    "groups": [
                        {
                            "clip_id": "clip_fresh",
                            "base_clip_id": "clip_fresh",
                            "representative_output_file": "clip_fresh.mp4",
                            "total_score": 9.0,
                            "scored_at": "2026-07-08T18:39:53+08:00",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        os.utime(fresh_dir, (1_900_000_000, 1_900_000_000))
        os.utime(self.run_dir, (1_700_000_000, 1_700_000_000))

        self.config.READ_APP_MAX_OUTPUT_DIRS = 2
        self.state_path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "videos": {
                        "vod": {
                            "name": "2026-05-15-10-52-43.mp4",
                            "path": "D:/VOD/2026-05-15-10-52-43.mp4",
                            "status": "completed",
                            "output_dir": str(fresh_dir),
                            "run_history": [{"output_dir": str(path)} for path in old_dirs],
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        output_dirs = self.service._collect_output_dirs()
        result = self.service.scores(limit=1)

        self.assertIn(str(fresh_dir), output_dirs)
        self.assertEqual(result.data.rows[0].clip_id, "clip_fresh")
        self.assertEqual(result.data.rows[0].scored_at, "2026-07-08T18:39:53+08:00")

    def test_compliance_index_is_manifest_cheap_and_detail_reads_json(self):
        index = self.service.compliance(limit=10)
        self.assertEqual(index.data.total, 1)
        self.assertEqual(index.data.summary["blocked"], 1)
        self.assertEqual(len(index.data.violations), 0)
        self.assertIn("blocked", index.data.filter_options["status"])

        detail = self.service.compliance_detail(str(self.run_dir))
        self.assertEqual(len(detail.data.violations), 1)
        self.assertEqual(detail.data.violations[0].violation_type, "claim")

    def test_modules_readiness_and_library_use_index(self):
        readiness = self.service.module_readiness()
        serum = [row for row in readiness.data.rows if row.product_key == "serum"][0]
        self.assertEqual(serum.readiness, "partial")
        self.assertEqual(serum.zoom_ready_candidates, 1)

        library = self.service.module_library(limit=10, product="serum")
        self.assertEqual(library.data.total, 1)
        self.assertEqual(library.data.rows[0].module_id, "module_001")
        self.assertNotIn("transcript_text", library.data.rows[0].model_dump())
        self.assertIn("approved", library.data.filter_options["quality_status"])

        detail = self.service.module_detail("module_001")
        self.assertEqual(detail.data.selected.module_id, "module_001")
        self.assertEqual(detail.data.transcript_text, "serum intro")

        filtered = self.service.module_library(limit=10, quality_status="approved", visual_status="passed")
        self.assertEqual(filtered.data.total, 1)

    def test_overview_is_compact_and_uses_cached_corpora(self):
        status_path = self.output_root / "export_batches" / "_status.json"
        status_path.parent.mkdir()
        status_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "status": "completed",
                    "updated_at": "2026-06-02T12:05:00+08:00",
                    "trigger": "automatic",
                    "actionable_count": 3,
                    "packaged_count": 2,
                    "pending_count": 1,
                    "packaged_total": 55,
                    "error_count": 0,
                    "batch_size": 15,
                    "dry_run": False,
                }
            ),
            encoding="utf-8",
        )
        latest_export = {
            "status": "completed",
            "updated_at": "2026-06-02T12:00:00+08:00",
            "result_summary": {
                "eligible_count": 10,
                "packaged_count": 4,
                "batch_size": 2,
                "dry_run": False,
            },
        }

        with (
            mock.patch.object(self.service, "_load_json_dict", wraps=self.service._load_json_dict) as loader,
            mock.patch.object(
                self.service,
                "_score_records",
                side_effect=AssertionError("Overview must not build the full score-row corpus"),
            ),
            mock.patch.object(
                self.service,
                "_compliance_records",
                side_effect=AssertionError("Overview must not build compliance detail rows"),
            ),
        ):
            first = self.service.overview(latest_export)
            first_load_count = loader.call_count
            second = self.service.overview(latest_export)

        self.assertEqual(first.data.revision, second.data.revision)
        self.assertEqual(loader.call_count, first_load_count)
        self.assertLessEqual(len(first.data.top_clips), 5)
        self.assertLessEqual(len(first.data.score_trend), 14)
        self.assertTrue(first.data.export.available)
        self.assertEqual(first.data.export.actionable, 3)
        self.assertEqual(first.data.export.packaged_last_run, 2)
        self.assertEqual(first.data.export.pending, 1)
        self.assertEqual(first.data.export.packaged_total, 55)
        self.assertEqual(first.data.export_ready_count, 1)
        self.assertNotIn("raw", first.data.model_dump())

    def test_overview_does_not_fall_back_to_scores_or_manual_job_without_status(self):
        latest_export = {
            "status": "completed",
            "updated_at": "2026-06-02T12:00:00+08:00",
            "result_summary": {"eligible_count": 12006, "packaged_count": 1},
        }

        result = self.service.overview(latest_export)

        self.assertFalse(result.data.export.available)
        self.assertEqual(result.data.export.pending, 0)
        self.assertEqual(result.data.export_ready_count, 0)

    def test_export_status_change_invalidates_overview_cache(self):
        status_path = self.output_root / "export_batches" / "_status.json"
        status_path.parent.mkdir()
        status_path.write_text(
            json.dumps({"status": "completed", "pending_count": 2, "actionable_count": 2}),
            encoding="utf-8",
        )
        first = self.service.overview()
        status_path.write_text(
            json.dumps({"status": "completed", "pending_count": 0, "actionable_count": 0}),
            encoding="utf-8",
        )

        second = self.service.overview()

        self.assertNotEqual(first.data.revision, second.data.revision)
        self.assertEqual(second.data.export.pending, 0)

    def test_settings_logs_and_artifact_safety(self):
        settings = self.service.settings_snapshot()
        self.assertIn("selection", settings.data.groups)

        logs = self.service.log_tail(str(self.working / "pipeline.log"), lines=2)
        self.assertEqual(logs.data.returned_lines, 2)
        self.assertEqual([line.text for line in logs.data.lines], ["three", "two"])
        self.assertEqual([line.line_number for line in logs.data.lines], [3, 2])

        artifact = self.service.resolve_artifact(str(self.run_dir / "clip_001.mp4"))
        self.assertTrue(artifact.path.exists())
        with self.assertRaises(PermissionError):
            self.service.resolve_artifact(str(self.root / "outside.mp4"))
        with self.assertRaises(FileNotFoundError):
            self.service.resolve_artifact(str(self.output_root / "missing.mp4"))

    def test_log_tail_handles_crlf_invalid_utf8_and_missing_final_newline(self):
        log_path = self.working / "pipeline.log"
        log_path.write_bytes(b"one\r\nbad:\xff\r\nlast")

        logs = self.service.log_tail(str(log_path), lines=3)

        self.assertEqual([line.text for line in logs.data.lines], ["last", "bad:\ufffd", "one"])
        self.assertEqual([line.line_number for line in logs.data.lines], [3, 2, 1])
        self.assertEqual(logs.data.total_lines, 3)
        self.assertEqual(logs.warnings, ())

    def test_log_tail_warns_when_read_cap_omits_partial_line(self):
        log_path = self.working / "pipeline.log"
        log_path.write_bytes(b"x" * ((4 * 1024 * 1024) + 1))

        logs = self.service.log_tail(str(log_path), lines=10)

        self.assertEqual(logs.data.returned_lines, 0)
        self.assertIsNone(logs.data.total_lines)
        self.assertIn("4 MiB", logs.warnings[0])

    def test_variation_preview_asset_root_is_allowed(self):
        asset_root = self.root / "assets" / "variation_preview"
        asset_root.mkdir(parents=True)
        preview_clip = asset_root / "raw_cut_preview.mp4"
        preview_clip.write_bytes(b"preview")

        with mock.patch("clipper_app.application.read_services.Path.cwd", return_value=self.root):
            artifact = self.service.resolve_artifact(str(preview_clip))

        self.assertEqual(artifact.path, preview_clip.resolve())
        self.assertEqual(artifact.media_type, "video/mp4")


if __name__ == "__main__":
    unittest.main()
