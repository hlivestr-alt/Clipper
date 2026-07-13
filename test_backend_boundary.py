import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pydantic import ValidationError

from clipper_app.application.events import InMemoryEventSink, LegacyCallbackEventSink
from clipper_app.application.services import (
    ComplianceService,
    HealthService,
    PipelineService,
    QueueControlService,
    QueueService,
    QueueSupervisorService,
    ScoringService,
)
from clipper_app.application.settings import LegacyConfigProvider
from clipper_app.contracts import (
    EventKind,
    PipelineRunCommand,
    QueueAction,
    QueueControlCommand,
    QueueLaunchConfig,
    QueueRunCommand,
    QueueSupervisorCommand,
    ScoringCommand,
    ComplianceScanCommand,
    Stage,
)


class BackendBoundaryTests(unittest.TestCase):
    def _config(self):
        return SimpleNamespace(
            OUTPUT_DIR="out",
            WORKING_DIR="working",
            QUEUE_INPUT_DIR="vod",
            QUEUE_STATE_FILE="working/state.json",
            QUEUE_FOREVER_STATE_FILE="working/forever.json",
            QUEUE_CONTROL_FILE="working/control.json",
            QUEUE_START_RUN_NUMBER=12,
            QUEUE_MAX_RETRIES=2,
            QUEUE_MAX_INFLIGHT_VIDEOS=1,
            QUEUE_FFMPEG_MAX_PARALLEL_CLIPS=4,
            QUEUE_YOLO_IN_SUBPROCESS=True,
            QUEUE_POLL_INTERVAL=2.0,
            QUEUE_RESCAN_INTERVAL_SECONDS=300.0,
            QUEUE_STABLE_SECONDS=60.0,
            MIN_SCORE=7.0,
            MAX_PARALLEL_CLIPS=4,
            OUTPUT_FPS=30,
            OUTPUT_CQ=26,
            DRAFT_MODE=False,
            OUTPUT_CODEC="h264_nvenc",
            OUTPUT_PRESET="p4",
        )

    def test_commands_are_strict_and_frozen(self):
        with self.assertRaises(ValidationError):
            PipelineRunCommand(video_path="vod.mp4", unexpected=True)
        command = PipelineRunCommand(video_path="vod.mp4")
        with self.assertRaises(ValidationError):
            command.max_clips = 3

    def test_settings_snapshot_validates_precedence_and_does_not_mutate_base(self):
        base = self._config()
        provider = LegacyConfigProvider(base)
        snapshot = provider.snapshot({"MIN_SCORE": 8.5, "MAX_PARALLEL_CLIPS": 2})
        runtime = provider.runtime_view(snapshot)

        self.assertEqual(snapshot.get("MIN_SCORE"), 8.5)
        self.assertEqual(runtime.MIN_SCORE, 8.5)
        runtime.MAX_PARALLEL_CLIPS = 1
        self.assertEqual(runtime.MAX_PARALLEL_CLIPS, 1)
        self.assertEqual(base.MAX_PARALLEL_CLIPS, 4)
        self.assertEqual(provider.snapshot().get("MIN_SCORE"), 7.0)

    def test_settings_snapshot_file_is_immutable_and_live_view_tracks_new_overrides(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            overrides_path = root / "settings_overrides.json"
            provider = LegacyConfigProvider(self._config(), overrides_path=overrides_path)
            overrides_path.write_text(
                json.dumps({"overrides": {"MIN_SCORE": 8.0}}),
                encoding="utf-8",
            )
            first = provider.snapshot()
            snapshot_path = root / "snapshots" / f"{first.revision}.json"
            provider.write_snapshot_file(first, snapshot_path)
            live = provider.live_view()
            self.assertEqual(live.MIN_SCORE, 8.0)

            overrides_path.write_text(
                json.dumps({"overrides": {"MIN_SCORE": 9.0}}),
                encoding="utf-8",
            )
            self.assertEqual(live.MIN_SCORE, 9.0)
            frozen = provider.snapshot_from_file(snapshot_path)
            self.assertEqual(frozen.get("MIN_SCORE"), 8.0)
            self.assertEqual(frozen.revision, first.revision)

    def test_queue_runner_records_frozen_settings_revision(self):
        from video_queue import VideoQueueRunner

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cfg = self._config()
            cfg.WORKING_DIR = str(root / "working")
            provider = LegacyConfigProvider(cfg, include_persisted_overrides=False)
            snapshot = provider.snapshot({"MIN_SCORE": 8.0})
            snapshot_path = root / "snapshot.json"
            provider.write_snapshot_file(snapshot, snapshot_path)
            input_dir = root / "input"
            input_dir.mkdir()
            runner = VideoQueueRunner(
                input_dir=str(input_dir),
                state_path=str(root / "state.json"),
                max_retries=0,
                max_inflight_videos=1,
                settings_snapshot_file=str(snapshot_path),
            )
            self.assertEqual(runner.cfg.MIN_SCORE, 8.0)
            self.assertEqual(runner.state["settings_revision"], snapshot.revision)

    def test_settings_snapshot_rejects_unknown_and_out_of_range_overrides(self):
        provider = LegacyConfigProvider(self._config())
        with self.assertRaisesRegex(ValueError, "Unsupported settings"):
            provider.snapshot({"WORD_CORRECTIONS": "unsafe"})
        with self.assertRaisesRegex(ValueError, "MIN_SCORE"):
            provider.snapshot({"MIN_SCORE": 11.0})

    def test_canonical_model_and_scan_settings_drive_legacy_aliases(self):
        base = self._config()
        base.LM_STUDIO_MOMENT_MODEL_ID = "text-old"
        base.LM_STUDIO_MODEL = "legacy-text"
        base.SCORER_VISION_MODEL_ID = "vision-old"
        base.SCORER_VISION_MODEL = "legacy-vision"
        base.QUEUE_RESCAN_INTERVAL_SECONDS = 300.0
        base.QUEUE_SCAN_INTERVAL_SECONDS = 10.0
        provider = LegacyConfigProvider(base)
        snapshot = provider.snapshot({
            "LM_STUDIO_MOMENT_MODEL_ID": "text-new",
            "SCORER_VISION_MODEL_ID": "vision-new",
            "QUEUE_RESCAN_INTERVAL_SECONDS": 90.0,
        })
        runtime = provider.runtime_view(snapshot)
        self.assertEqual(runtime.LM_STUDIO_MODEL, "text-new")
        self.assertEqual(runtime.SCORER_VISION_MODEL, "vision-new")
        self.assertEqual(runtime.QUEUE_SCAN_INTERVAL_SECONDS, 90.0)

    def test_stage_fingerprint_treats_numeric_snapshot_values_equivalently(self):
        from stage_cache import stage_fingerprint

        legacy_cfg = SimpleNamespace(
            OUTPUT_CODEC="h264_nvenc",
            COMPLIANCE_LM_TIMEOUT=60,
        )
        service_cfg = SimpleNamespace(
            OUTPUT_CODEC="h264_nvenc",
            COMPLIANCE_LM_TIMEOUT=60.0,
        )

        self.assertEqual(
            stage_fingerprint("missing.mp4", legacy_cfg, "ffmpeg"),
            stage_fingerprint("missing.mp4", service_cfg, "ffmpeg"),
        )

    def test_pipeline_service_emits_typed_events_and_returns_typed_result(self):
        provider = LegacyConfigProvider(self._config())
        observed = {}

        def executor(command, runtime_cfg, progress_callback):
            observed["command"] = command
            observed["score"] = runtime_cfg.MIN_SCORE
            progress_callback(
                "editing",
                75,
                "Rendered clip",
                event="clip_complete",
                clip_id="clip_001",
                clip_status="ok",
                clips_total=2,
                clips_completed=1,
                manifest_path="out/manifest.json",
            )
            return {
                "clips_created": 1,
                "clips_failed": 0,
                "clips_skipped": 0,
                "clips_blocked": 0,
                "moments_found": 2,
                "total_time": 1.5,
                "output_dir": "out",
                "manifest_path": "out/manifest.json",
                "scores_summary_path": None,
                "clips_scored": 0,
                "export_batches": {},
                "module_extraction": {},
                "modular_assembly": {},
            }

        service = PipelineService(executor=executor, settings_provider=provider)
        sink = InMemoryEventSink()
        result = service.run(
            PipelineRunCommand(video_path="vod.mp4", settings_overrides={"MIN_SCORE": 8.0}),
            sink,
        )

        self.assertEqual(result.clips_created, 1)
        self.assertEqual(observed["score"], 8.0)
        self.assertEqual(len(sink.events), 1)
        event = sink.events[0]
        self.assertEqual(event.kind, EventKind.CLIP_COMPLETE)
        self.assertEqual(event.stage, Stage.EDITING)
        self.assertEqual(event.metrics.clips_completed, 1)
        self.assertEqual(event.artifacts.manifest_path, "out/manifest.json")

    def test_legacy_event_sink_preserves_callback_shape(self):
        calls = []
        sink = LegacyCallbackEventSink(lambda *args, **kwargs: calls.append((args, kwargs)))
        provider = LegacyConfigProvider(self._config())

        def executor(command, runtime_cfg, progress_callback):
            progress_callback("done", 100, "Finished", event="pipeline_complete", clips_created=1)
            return {"clips_created": 1}

        PipelineService(executor, provider).run(PipelineRunCommand(video_path="vod.mp4"), sink)

        args, kwargs = calls[0]
        self.assertEqual(args, ("done", 100, "Finished"))
        self.assertEqual(kwargs["event"], "pipeline_complete")
        self.assertEqual(kwargs["clips_created"], 1)

    def test_queue_control_service_preserves_control_schema(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            control_path = Path(temp_dir) / "control.json"
            service = QueueControlService()
            snapshot = service.execute(
                QueueControlCommand(action=QueueAction.STOP, control_path=str(control_path))
            )

            persisted = json.loads(control_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["schema_version"], 1)
            self.assertEqual(persisted["requested_action"], "stop_graceful")
            self.assertEqual(snapshot.control["status"], "stop_requested")

    def test_queue_launch_config_normalizes_max_clips_and_raw_cut_variants(self):
        config = QueueLaunchConfig(
            pipeline_mode="raw_cuts_only",
            variant_mode="custom",
            variant_count=6,
            max_clips=0,
        )
        self.assertEqual(config.pipeline_mode, "raw_cuts_only")
        self.assertEqual(config.variant_mode, "original")
        self.assertEqual(config.variant_count, 1)
        self.assertIsNone(config.max_clips)

        with self.assertRaises(ValidationError):
            QueueLaunchConfig(run_mode="single_video")

        with self.assertRaises(ValidationError):
            QueueLaunchConfig(run_mode="folder_once", video_path="vod.mp4")

    def test_queue_control_stop_clears_pending_queue_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            control_path = root / "control.json"
            state_path = root / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "queue_status": "running",
                        "videos": {
                            "vod": {
                                "status": "queued",
                                "current_stage": None,
                                "stages": {
                                    "transcribe": {"status": "queued", "queued": True},
                                    "llm": {"status": "pending"},
                                    "yolo": {"status": "pending"},
                                    "ffmpeg": {"status": "pending"},
                                },
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            QueueControlService().execute(
                QueueControlCommand(
                    action=QueueAction.STOP,
                    control_path=str(control_path),
                    queue_state_path=str(state_path),
                )
            )

            persisted = json.loads(state_path.read_text(encoding="utf-8"))
            video = persisted["videos"]["vod"]
            self.assertEqual(persisted["queue_status"], "stopped")
            self.assertEqual(video["status"], "stopped")
            self.assertEqual(video["stages"]["transcribe"]["status"], "skipped")

    def test_scoring_and_compliance_services_pass_local_settings(self):
        provider = LegacyConfigProvider(self._config())
        with mock.patch("clip_scorer.score_output_tree", return_value=[{"clip_id": "c1"}]) as score:
            result = ScoringService(provider).rescore(ScoringCommand(
                output_dir="out",
                force_rescore=True,
            ))
        self.assertEqual(result.scores[0]["clip_id"], "c1")
        self.assertTrue(score.call_args.kwargs["cfg"].SCORER_FORCE_RESCORE)

        compliance_payload = {
            "output_dir": "out",
            "manifest_path": "out/manifest.json",
            "scanned": 2,
            "passed": 1,
            "blocked": 1,
            "auto_fixed": 1,
            "violation_count": 3,
        }
        with mock.patch("compliance_checker.scan_output_dir", return_value=compliance_payload) as scan:
            compliance = ComplianceService(provider).scan(ComplianceScanCommand(output_dir="out"))
        self.assertEqual(compliance.violation_count, 3)
        self.assertIsNotNone(scan.call_args.kwargs["cfg"])

    def test_queue_and_health_services_keep_existing_execution_contracts(self):
        observed = {}

        class Runner:
            def run(self):
                return 10

        def factory(command):
            observed["command"] = command
            return Runner()

        result = QueueService(factory).run(QueueRunCommand(
            input_dir="vod",
            state_path="working/state.json",
        ))
        self.assertEqual(result.exit_code, 10)
        self.assertEqual(observed["command"].max_inflight_videos, 1)
        self.assertEqual(observed["command"].stage_admission_limit, 3)

        with mock.patch("queue_state_health.derive_queue_health", return_value={"status": "ok"}) as derive:
            health = HealthService().snapshot({}, running_stall_seconds=5)
        self.assertEqual(health["status"], "ok")
        derive.assert_called_once_with({}, running_stall_seconds=5)

    def test_queue_control_start_launches_missing_supervisor(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            service = QueueControlService()
            process = SimpleNamespace(pid=12345)

            with mock.patch.object(service, "_process_command_lines", return_value=[]), \
                mock.patch("clipper_app.application.services.subprocess.Popen", return_value=process) as popen:
                snapshot = service.execute(QueueControlCommand(
                    action=QueueAction.START,
                    control_path=str(tmp_path / "queue_control.json"),
                    forever_state_path=str(tmp_path / "queue_forever_state.json"),
                    queue_state_path=str(tmp_path / "video_queue_state.json"),
                ))

            popen.assert_called_once()
            self.assertEqual(snapshot.control["requested_action"], "run")
            self.assertEqual(snapshot.control["status"], "start_requested")
            self.assertTrue(snapshot.control["supervisor_launch"]["started"])
            self.assertEqual(snapshot.control["supervisor_launch"]["pid"], 12345)
            self.assertEqual(snapshot.control["supervisor_launch"]["replaced_pids"], [])

    def test_queue_control_rotates_supervisor_log_before_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            working = tmp_path / "working"
            working.mkdir()
            launch_log = working / "queue_supervisor_launch.log"
            launch_log.write_bytes(b"old-line\n" * 20)
            cfg = self._config()
            cfg.WORKING_DIR = str(working)
            service = QueueControlService(
                LegacyConfigProvider(cfg, include_persisted_overrides=False)
            )
            process = SimpleNamespace(pid=12345)

            with mock.patch.object(service, "_process_command_lines", return_value=[]), \
                mock.patch("clipper_app.application.services.SUPERVISOR_LOG_MAX_BYTES", 64), \
                mock.patch("clipper_app.application.services.subprocess.Popen", return_value=process):
                service.execute(QueueControlCommand(
                    action=QueueAction.START,
                    control_path=str(tmp_path / "queue_control.json"),
                    forever_state_path=str(tmp_path / "queue_forever_state.json"),
                    queue_state_path=str(tmp_path / "video_queue_state.json"),
                ))

            backup = working / "queue_supervisor_launch.log.1"
            self.assertTrue(backup.exists())
            self.assertLessEqual(backup.stat().st_size, 64)
            self.assertTrue(backup.read_bytes().startswith(b"old-line\n"))

    def test_queue_control_start_passes_launch_config_to_supervisor(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            input_dir = tmp_path / "vod"
            input_dir.mkdir()
            video = input_dir / "a.mp4"
            video.write_bytes(b"video")
            service = QueueControlService()
            process = SimpleNamespace(pid=12345)

            with mock.patch("config.QUEUE_INPUT_DIR", str(input_dir), create=True), \
                mock.patch.object(service, "_process_command_lines", return_value=[]), \
                mock.patch("clipper_app.application.services.subprocess.Popen", return_value=process) as popen:
                snapshot = service.execute(QueueControlCommand(
                    action=QueueAction.START,
                    control_path=str(tmp_path / "queue_control.json"),
                    forever_state_path=str(tmp_path / "queue_forever_state.json"),
                    queue_state_path=str(tmp_path / "video_queue_state.json"),
                    launch_config=QueueLaunchConfig(
                        run_mode="single_video",
                        pipeline_mode="raw_cuts_only",
                        variant_mode="custom",
                        variant_count=5,
                        max_clips=0,
                        video_path=str(video),
                    ),
                ))

            command_line = popen.call_args.args[0]
            self.assertIn("--run-mode", command_line)
            self.assertIn("single_video", command_line)
            self.assertIn("--pipeline-mode", command_line)
            self.assertIn("raw_cuts_only", command_line)
            self.assertIn("--variant-mode", command_line)
            self.assertIn("original", command_line)
            self.assertIn("--variant-count", command_line)
            self.assertIn("1", command_line)
            self.assertIn("--video-path", command_line)
            self.assertIn(str(video.resolve()), command_line)
            self.assertNotIn("--max-clips", command_line)
            self.assertEqual(snapshot.control["launch_config"]["variant_mode"], "original")

    def test_queue_control_continue_uses_running_supervisor(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            service = QueueControlService()

            with mock.patch.object(service, "_process_command_lines", return_value=[]), \
                mock.patch.object(service, "_matching_supervisor_pids", return_value=[777]), \
                mock.patch.object(service, "_video_queue_process_running", return_value=True), \
                mock.patch("clipper_app.application.services.subprocess.Popen") as popen:
                snapshot = service.execute(QueueControlCommand(
                    action=QueueAction.CONTINUE,
                    control_path=str(tmp_path / "queue_control.json"),
                    forever_state_path=str(tmp_path / "queue_forever_state.json"),
                    queue_state_path=str(tmp_path / "video_queue_state.json"),
                ))

            popen.assert_not_called()
            self.assertEqual(snapshot.control["requested_action"], "run")
            self.assertEqual(snapshot.control["status"], "continue_requested")
            self.assertEqual(
                snapshot.control["supervisor_launch"],
                {"started": False, "reason": "supervisor_already_running", "pids": [777]},
            )

    def test_queue_control_continue_replaces_idle_supervisor(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            service = QueueControlService()
            process = SimpleNamespace(pid=12345)

            with mock.patch.object(service, "_process_command_lines", return_value=[]), \
                mock.patch.object(service, "_matching_supervisor_pids", return_value=[777]), \
                mock.patch.object(service, "_video_queue_process_running", return_value=False), \
                mock.patch.object(service, "_terminate_processes", return_value=[777]) as terminate, \
                mock.patch("clipper_app.application.services.subprocess.Popen", return_value=process) as popen:
                snapshot = service.execute(QueueControlCommand(
                    action=QueueAction.CONTINUE,
                    control_path=str(tmp_path / "queue_control.json"),
                    forever_state_path=str(tmp_path / "queue_forever_state.json"),
                    queue_state_path=str(tmp_path / "video_queue_state.json"),
                ))

            terminate.assert_called_once_with([777])
            popen.assert_called_once()
            self.assertTrue(snapshot.control["supervisor_launch"]["started"])
            self.assertEqual(snapshot.control["supervisor_launch"]["replaced_pids"], [777])

    def test_public_pipeline_facade_supports_service_and_legacy_modes(self):
        import main

        payload = {"clips_created": 1, "output_dir": "out"}
        with mock.patch.object(main, "_run_pipeline_impl", return_value=payload) as executor, \
            mock.patch.dict(os.environ, {"CLIPPER_SERVICE_BOUNDARY": "service"}):
            result = main.run_pipeline("vod.mp4", max_clips=1)
        self.assertEqual(result["clips_created"], 1)
        self.assertIsNotNone(executor.call_args.kwargs["runtime_cfg"])

        with mock.patch.object(main, "_run_pipeline_impl", return_value=payload) as executor, \
            mock.patch.dict(os.environ, {"CLIPPER_SERVICE_BOUNDARY": "legacy"}):
            result = main.run_pipeline("vod.mp4", max_clips=1)
        self.assertEqual(result, payload)
        self.assertNotIn("runtime_cfg", executor.call_args.kwargs)

    def test_video_queue_cli_builds_typed_command_without_changing_flags(self):
        import video_queue

        observed = {}

        class Runner:
            def run(self):
                return 0

        def factory(command):
            observed["command"] = command
            return Runner()

        argv = [
            "video_queue.py",
            "--input-dir", "vod",
            "--state-file", "state.json",
            "--max-retries", "3",
            "--max-inflight-videos", "2",
            "--ffmpeg-max-parallel-clips", "1",
            "--stage-admission-limit", "5",
            "--max-clips", "4",
            "--min-score", "8",
            "--redo-tag", "run_001",
        ]
        with mock.patch.object(sys, "argv", argv), \
            mock.patch.object(video_queue, "_runner_from_command", side_effect=factory), \
            mock.patch.dict(os.environ, {"CLIPPER_SERVICE_BOUNDARY": "service"}):
            exit_code = video_queue.main()

        self.assertEqual(exit_code, 0)
        command = observed["command"]
        self.assertEqual(command.max_retries, 3)
        self.assertEqual(command.max_inflight_videos, 2)
        self.assertEqual(command.stage_admission_limit, 5)
        self.assertEqual(command.output_tag, "run_001")
        self.assertEqual(command.working_tag, "run_001")

    def test_supervisor_parser_maps_to_typed_service_command(self):
        import queue_supervisor

        args = queue_supervisor.parse_args([
            "--input-dir", "vod",
            "--state-file", "state.json",
            "--forever-state-file", "forever.json",
            "--control-file", "control.json",
            "--start-run-number", "9",
            "--max-retries", "4",
            "--stage-admission-limit", "6",
            "--dry-run",
        ])
        command = QueueSupervisorCommand(**vars(args))
        observed = {}

        def executor(value):
            observed["command"] = value
            return 10

        result = QueueSupervisorService(executor).run(command)
        self.assertEqual(result.exit_code, 10)
        self.assertEqual(observed["command"].start_run_number, 9)
        self.assertEqual(observed["command"].max_retries, 4)
        self.assertEqual(observed["command"].stage_admission_limit, 6)


if __name__ == "__main__":
    unittest.main()
