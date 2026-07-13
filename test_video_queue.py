import json
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import queue_control
from clipper_app.application.settings import LegacyConfigProvider
from video_queue import (
    EDIT_STAGE,
    PRE_EDIT_STAGES,
    STAGES,
    StageJob,
    VideoQueueRunner,
    _build_versioned_stem,
    _reuse_base_transcript_for_tagged_run,
    clear_pending_queue_state,
)


class VideoQueueSchedulingTests(unittest.TestCase):
    def test_leading_separator_tag_builds_operator_run_folder_name(self):
        self.assertEqual(_build_versioned_stem("video", "_run_001"), "video_run_001")
        self.assertEqual(_build_versioned_stem("video", "run_001"), "video__run_001")

    def test_tagged_redo_reuses_previous_tagged_transcript(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            video = temp_path / "input" / "a.mp4"
            video.parent.mkdir()
            video.write_bytes(b"")

            working_root = temp_path / "working"
            previous_working_dir = working_root / "a__redo_old"
            target_working_dir = working_root / "a__redo_new"
            previous_working_dir.mkdir(parents=True)

            transcript = {
                "segments": [{"id": 0, "start": 0.0, "end": 1.0, "text": "hello", "words": []}],
                "words": [{"word": "hello", "start": 0.0, "end": 0.5}],
                "metadata": {
                    "schema_version": 3,
                    "word_alignment_backend": "whisperx",
                    "source_video_path": str(video.resolve()),
                },
            }
            with open(previous_working_dir / "transcript.json", "w", encoding="utf-8") as f:
                json.dump(transcript, f)
            (previous_working_dir / "transcript.raw_checkpoint.json").write_text(
                '{"checkpoint": true}',
                encoding="utf-8",
            )

            class Cfg:
                WORKING_DIR = str(working_root)
                WORD_ALIGNMENT_BACKEND = "whisperx"
                WHISPERX_ACCEPT_RAW_FALLBACK_CACHE = True

            reused = _reuse_base_transcript_for_tagged_run(
                str(video),
                target_working_dir,
                "redo_new",
                Cfg,
            )

            self.assertTrue(reused)
            self.assertTrue((target_working_dir / "transcript.json").exists())
            self.assertTrue((target_working_dir / "transcript.raw_checkpoint.json").exists())

    def test_yolo_queue_backfills_next_transcribe_before_scan_finishes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            input_dir = temp_path / "input"
            input_dir.mkdir()
            first_video = input_dir / "a.mp4"
            second_video = input_dir / "b.mp4"
            first_video.write_bytes(b"")
            second_video.write_bytes(b"")

            runner = VideoQueueRunner(
                input_dir=str(input_dir),
                state_path=str(temp_path / "state.json"),
                max_retries=0,
                max_inflight_videos=1,
                poll_interval=0.5,
                control_path=str(temp_path / "control.json"),
            )
            runner._sync_videos([first_video, second_video])

            first_key = str(first_video.resolve())
            second_key = str(second_video.resolve())

            with runner.state_lock:
                first_entry = runner.state["videos"][first_key]
                first_entry["status"] = "queued"
                first_entry["stages"]["transcribe"]["status"] = "done"
                first_entry["stages"]["llm"]["status"] = "done"
                first_entry["stages"]["yolo"]["status"] = "pending"
                first_entry["stages"]["ffmpeg"]["status"] = "pending"

                second_entry = runner.state["videos"][second_key]
                second_entry["status"] = "queued"
                for stage in STAGES:
                    second_entry["stages"][stage]["status"] = "pending"
                    second_entry["stages"][stage]["queued"] = False

                runner._schedule_locked("unit-test")

                self.assertEqual(first_entry["stages"]["yolo"]["status"], "queued")
                self.assertEqual(second_entry["stages"]["transcribe"]["status"], "queued")

    def test_yolo_start_backfills_next_transcribe_from_existing_queue_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            input_dir = temp_path / "input"
            input_dir.mkdir()
            first_video = input_dir / "a.mp4"
            second_video = input_dir / "b.mp4"
            first_video.write_bytes(b"")
            second_video.write_bytes(b"")

            runner = VideoQueueRunner(
                input_dir=str(input_dir),
                state_path=str(temp_path / "state.json"),
                max_retries=0,
                max_inflight_videos=1,
                poll_interval=0.5,
                control_path=str(temp_path / "control.json"),
            )
            runner._sync_videos([first_video, second_video])

            first_key = str(first_video.resolve())
            second_key = str(second_video.resolve())
            observed_second_transcribe_statuses = []

            def fake_execute(job):
                with runner.state_lock:
                    observed_second_transcribe_statuses.append(
                        runner.state["videos"][second_key]["stages"]["transcribe"]["status"]
                    )

            runner._execute_stage = fake_execute

            with runner.state_lock:
                first_entry = runner.state["videos"][first_key]
                first_entry["status"] = "queued"
                first_entry["stages"]["transcribe"]["status"] = "done"
                first_entry["stages"]["llm"]["status"] = "done"
                first_entry["stages"]["yolo"]["status"] = "queued"
                first_entry["stages"]["yolo"]["queued"] = True

                second_entry = runner.state["videos"][second_key]
                second_entry["status"] = "queued"
                for stage in STAGES:
                    second_entry["stages"][stage]["status"] = "pending"
                    second_entry["stages"][stage]["queued"] = False

            runner._run_job("yolo-worker", StageJob(video_path=first_key, stage="yolo"))

            self.assertEqual(observed_second_transcribe_statuses, ["queued"])

    def test_ffmpeg_start_backfills_next_transcribe_before_edit_finishes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            input_dir = temp_path / "input"
            input_dir.mkdir()
            first_video = input_dir / "a.mp4"
            second_video = input_dir / "b.mp4"
            first_video.write_bytes(b"")
            second_video.write_bytes(b"")

            runner = VideoQueueRunner(
                input_dir=str(input_dir),
                state_path=str(temp_path / "state.json"),
                max_retries=0,
                max_inflight_videos=1,
                poll_interval=0.5,
                control_path=str(temp_path / "control.json"),
            )
            runner._sync_videos([first_video, second_video])

            first_key = str(first_video.resolve())
            second_key = str(second_video.resolve())
            observed_second_transcribe_statuses = []

            def fake_execute(job):
                with runner.state_lock:
                    observed_second_transcribe_statuses.append(
                        runner.state["videos"][second_key]["stages"]["transcribe"]["status"]
                    )

            runner._execute_stage = fake_execute

            with runner.state_lock:
                first_entry = runner.state["videos"][first_key]
                first_entry["status"] = "queued"
                for stage in PRE_EDIT_STAGES:
                    first_entry["stages"][stage]["status"] = "done"
                first_entry["stages"][EDIT_STAGE]["status"] = "queued"
                first_entry["stages"][EDIT_STAGE]["queued"] = True

                second_entry = runner.state["videos"][second_key]
                second_entry["status"] = "queued"
                for stage in STAGES:
                    second_entry["stages"][stage]["status"] = "pending"
                    second_entry["stages"][stage]["queued"] = False

            runner._run_job("ffmpeg-worker", StageJob(video_path=first_key, stage=EDIT_STAGE))

            self.assertEqual(observed_second_transcribe_statuses, ["queued"])
            with runner.state_lock:
                self.assertEqual(
                    runner.state["videos"][second_key]["stages"]["transcribe"]["status"],
                    "queued",
                )

    def test_single_video_discovery_only_uses_selected_vod(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            input_dir.mkdir()
            first = input_dir / "a.mp4"
            second = input_dir / "b.mp4"
            first.write_bytes(b"video")
            second.write_bytes(b"video")

            runner = VideoQueueRunner(
                input_dir=str(input_dir),
                state_path=str(root / "state.json"),
                max_retries=0,
                max_inflight_videos=1,
                stable_seconds=0,
                control_path=str(root / "control.json"),
                run_mode="single_video",
                video_path=str(first),
            )

            self.assertEqual([path.resolve() for path in runner._discover_videos()], [first.resolve()])

    def test_large_folder_admits_only_stage_limit_and_leaves_rest_waiting(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            input_dir.mkdir()
            videos = []
            for index in range(8):
                video = input_dir / f"vod_{index:02d}.mp4"
                video.write_bytes(b"video")
                videos.append(video)

            runner = VideoQueueRunner(
                input_dir=str(input_dir),
                state_path=str(root / "state.json"),
                max_retries=0,
                max_inflight_videos=8,
                poll_interval=0.5,
                stable_seconds=0,
                control_path=str(root / "control.json"),
                pipeline_mode="clips_only",
                stage_admission_limit=3,
            )
            runner._sync_videos(videos)

            with runner.state_lock:
                runner._schedule_locked("unit-test")
                entries = list(runner.state["videos"].values())
                queued = [
                    entry for entry in entries
                    if entry["stages"][EDIT_STAGE]["status"] == "queued"
                ]
                waiting = [
                    entry for entry in entries
                    if entry["status"] == "waiting"
                    and entry["stages"][EDIT_STAGE]["status"] == "pending"
                    and not entry["stages"][EDIT_STAGE]["queued"]
                ]

            self.assertEqual(len(entries), 8)
            self.assertEqual(len(queued), 3)
            self.assertEqual(len(waiting), 5)
            self.assertEqual(runner.queues["ffmpeg"].qsize(), 3)

    def test_stage_admission_refills_one_waiting_video_after_completion(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            input_dir.mkdir()
            videos = []
            for index in range(8):
                video = input_dir / f"vod_{index:02d}.mp4"
                video.write_bytes(b"video")
                videos.append(video)

            runner = VideoQueueRunner(
                input_dir=str(input_dir),
                state_path=str(root / "state.json"),
                max_retries=0,
                max_inflight_videos=8,
                poll_interval=0.5,
                stable_seconds=0,
                control_path=str(root / "control.json"),
                pipeline_mode="clips_only",
                stage_admission_limit=3,
            )
            runner._sync_videos(videos)

            with runner.state_lock:
                runner._schedule_locked("bootstrap")
                first_payload = runner.queues["ffmpeg"].get_nowait()
                runner.queues["ffmpeg"].task_done()
                first_job = first_payload[2]
                first_entry = runner.state["videos"][first_job.video_path]
                first_entry["stages"][EDIT_STAGE]["status"] = "done"
                first_entry["stages"][EDIT_STAGE]["queued"] = False
                first_entry["stages"][EDIT_STAGE]["queued_at"] = None
                first_entry["status"] = "completed"
                first_entry["completed_at"] = runner._now_iso()

                runner._schedule_locked("ffmpeg-complete")
                entries = list(runner.state["videos"].values())
                queued = [
                    entry for entry in entries
                    if entry["stages"][EDIT_STAGE]["status"] == "queued"
                ]
                waiting = [
                    entry for entry in entries
                    if entry["status"] == "waiting"
                    and entry["stages"][EDIT_STAGE]["status"] == "pending"
                    and not entry["stages"][EDIT_STAGE]["queued"]
                ]

            self.assertEqual(len(queued), 3)
            self.assertEqual(len(waiting), 4)
            self.assertEqual(runner.queues["ffmpeg"].qsize(), 3)

    def test_stage_admission_stops_before_underlying_queue_full_warning(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            input_dir.mkdir()
            videos = []
            for index in range(8):
                video = input_dir / f"vod_{index:02d}.mp4"
                video.write_bytes(b"video")
                videos.append(video)

            runner = VideoQueueRunner(
                input_dir=str(input_dir),
                state_path=str(root / "state.json"),
                max_retries=0,
                max_inflight_videos=8,
                poll_interval=0.5,
                stable_seconds=0,
                control_path=str(root / "control.json"),
                pipeline_mode="clips_only",
                stage_admission_limit=3,
            )
            runner._sync_videos(videos)

            with runner.state_lock, \
                    mock.patch.object(runner.queues["ffmpeg"], "put_nowait", wraps=runner.queues["ffmpeg"].put_nowait) as put_nowait, \
                    mock.patch("video_queue.log.warning") as warning:
                runner._schedule_locked("unit-test")

            self.assertEqual(put_nowait.call_count, 3)
            warning.assert_not_called()

    def test_clips_only_skips_analysis_stages_and_starts_at_ffmpeg(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            input_dir.mkdir()
            video = input_dir / "a.mp4"
            video.write_bytes(b"video")

            runner = VideoQueueRunner(
                input_dir=str(input_dir),
                state_path=str(root / "state.json"),
                max_retries=0,
                max_inflight_videos=1,
                stable_seconds=0,
                control_path=str(root / "control.json"),
                pipeline_mode="clips_only",
                scan_once=True,
            )
            runner._sync_videos([video])
            key = str(video.resolve())

            with runner.state_lock:
                entry = runner.state["videos"][key]
                self.assertEqual(entry["stages"]["transcribe"]["status"], "skipped")
                self.assertEqual(entry["stages"]["llm"]["status"], "skipped")
                self.assertEqual(entry["stages"]["yolo"]["status"], "skipped")
                self.assertEqual(runner._next_stage_locked(entry), EDIT_STAGE)
                runner._schedule_locked("unit-test")
                self.assertEqual(entry["stages"][EDIT_STAGE]["status"], "queued")

    def test_raw_cuts_force_original_variant_and_disable_side_effects(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            input_dir.mkdir()

            runner = VideoQueueRunner(
                input_dir=str(input_dir),
                state_path=str(root / "state.json"),
                max_retries=0,
                max_inflight_videos=1,
                control_path=str(root / "control.json"),
                pipeline_mode="raw_cuts_only",
                variant_mode="custom",
                variant_count=6,
            )
            overrides = runner._pipeline_settings_overrides()

            self.assertEqual(runner.variant_mode, "original")
            self.assertEqual(overrides["VARIANTS_PER_CLIP"], 1)
            self.assertFalse(overrides["SCORER_ENABLED"])
            self.assertFalse(overrides["COMPLIANCE_ENABLED"])
            self.assertFalse(overrides["EXPORT_BATCHES_ENABLED"])
            snapshot = LegacyConfigProvider(types.SimpleNamespace(WORKING_DIR=str(root / "working"))).snapshot(overrides)
            self.assertFalse(snapshot.get("EXPORT_BATCHES_ENABLED"))
            self.assertFalse(snapshot.get("HOST_FACE_ZOOM_ENABLED"))
            self.assertFalse(snapshot.get("BEFORE_AFTER_ENABLED"))

    def test_stuck_stage_detector_repairs_stale_running_marker(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "input"
            input_dir.mkdir()
            video = input_dir / "a.mp4"
            video.write_bytes(b"video")

            runner = VideoQueueRunner(
                input_dir=str(input_dir),
                state_path=str(root / "state.json"),
                max_retries=0,
                max_inflight_videos=1,
                stable_seconds=0,
                control_path=str(root / "control.json"),
            )
            runner._sync_videos([video])
            key = str(video.resolve())
            old = "2000-01-01T00:00:00+00:00"

            with runner.state_lock:
                entry = runner.state["videos"][key]
                entry["status"] = "running"
                entry["current_stage"] = "transcribe"
                entry["stages"]["transcribe"]["status"] = "running"
                entry["stages"]["transcribe"]["started_at"] = old
                repaired = runner._repair_stuck_stages_locked("unit-test")

                self.assertEqual(repaired, 1)
                self.assertIn("Reset stale running stage", entry["stages"]["transcribe"]["last_error"])

    def test_clear_pending_queue_state_marks_pending_work_stopped(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
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

            result = clear_pending_queue_state(state_path)
            payload = json.loads(state_path.read_text(encoding="utf-8"))

            self.assertGreaterEqual(result["changed"], 1)
            self.assertEqual(payload["queue_status"], "stopped")
            self.assertEqual(payload["videos"]["vod"]["status"], "stopped")
            self.assertEqual(payload["videos"]["vod"]["stages"]["transcribe"]["status"], "skipped")

    def test_ffmpeg_progress_callback_persists_clip_counts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            input_dir = temp_path / "input"
            input_dir.mkdir()
            video = input_dir / "a.mp4"
            video.write_bytes(b"")
            state_path = temp_path / "state.json"

            runner = VideoQueueRunner(
                input_dir=str(input_dir),
                state_path=str(state_path),
                max_retries=0,
                max_inflight_videos=1,
                poll_interval=0.5,
            )
            runner._sync_videos([video])

            video_key = str(video.resolve())
            runner._handle_ffmpeg_progress(
                video_key,
                "editing",
                65,
                "[2/5] clip_002",
                event="clip_complete",
                clip_id="clip_002",
                clip_status="ok",
                clips_total=5,
                clips_completed=2,
                clips_created=2,
                clips_failed=0,
                clips_skipped=0,
            )

            persisted = json.loads(state_path.read_text(encoding="utf-8"))
            stage_state = persisted["videos"][video_key]["stages"][EDIT_STAGE]
            self.assertEqual(stage_state["progress_pct"], 65)
            self.assertEqual(stage_state["clips_total"], 5)
            self.assertEqual(stage_state["clips_completed"], 2)
            self.assertEqual(stage_state["clips_created"], 2)
            self.assertEqual(stage_state["last_clip_id"], "clip_002")
            self.assertEqual(stage_state["last_clip_status"], "ok")
            self.assertEqual(persisted["videos"][video_key]["current_stage"], EDIT_STAGE)

    def test_queue_ffmpeg_runs_export_batch_packaging_asynchronously(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            input_dir = temp_path / "input"
            input_dir.mkdir()
            video = input_dir / "a.mp4"
            video.write_bytes(b"")

            runner = VideoQueueRunner(
                input_dir=str(input_dir),
                state_path=str(temp_path / "state.json"),
                max_retries=0,
                max_inflight_videos=1,
                poll_interval=0.5,
                control_path=str(temp_path / "control.json"),
            )

            observed_kwargs = {}
            observed_result = {}
            started = threading.Event()
            release = threading.Event()
            finished = threading.Event()

            def fake_run_pipeline(**kwargs):
                observed_kwargs.update(kwargs)
                import main

                class Cfg:
                    EXPORT_BATCHES_ENABLED = True
                    EXPORT_BATCH_SIZE = 15
                    EXPORT_BATCH_TIMEOUT_SECONDS = 30
                    OUTPUT_DIR = str(temp_path / "output")

                observed_result.update(main._package_export_batches_if_enabled(Cfg))

            fake_export_packager = types.ModuleType("export_packager")

            def fake_package_export_batches(output_root, cfg=None, batch_size=None, trigger="direct"):
                self.assertEqual(trigger, "automatic")
                started.set()
                if not release.wait(timeout=5.0):
                    raise RuntimeError("test timed out waiting for release")
                finished.set()
                return {
                    "eligible_count": 3,
                    "new_unique_count": 3,
                    "packaged_count": 3,
                    "duplicate_existing_count": 0,
                    "duplicate_candidate_count": 0,
                    "error_count": 0,
                    "manifest_path": str(temp_path / "output" / "export_batches" / "_manifest.json"),
                    "assignments": [
                        {"batch_folder": "1"},
                        {"batch_folder": "1"},
                        {"batch_folder": "2"},
                    ],
                }

            fake_export_packager.package_export_batches = fake_package_export_batches

            with mock.patch.dict(sys.modules, {"export_packager": fake_export_packager}), \
                    mock.patch("main.run_pipeline", side_effect=fake_run_pipeline):
                start_time = time.perf_counter()
                runner._stage_ffmpeg(str(video))
                elapsed = time.perf_counter() - start_time

                self.assertLess(elapsed, 1.0)
                self.assertTrue(started.wait(timeout=1.0))
                self.assertFalse(finished.is_set())
                self.assertTrue(observed_result["async"])
                self.assertNotIn("package_export_batches", observed_kwargs)
                self.assertEqual(observed_kwargs["video_path"], str(video))
            release.set()
            self.assertTrue(finished.wait(timeout=2.0))

    def test_manual_max_clips_run_skips_synchronous_export_batch_packaging(self):
        import main

        class Cfg:
            EXPORT_BATCHES_ENABLED = True
            EXPORT_BATCH_SIZE = 15
            OUTPUT_DIR = "unused"

        fake_export_packager = types.ModuleType("export_packager")
        with mock.patch.dict("os.environ", {"PROYA_QUEUE_EXPORT_PACKAGING_ASYNC": ""}), \
                mock.patch.dict(sys.modules, {"export_packager": fake_export_packager}):
            result = main._package_export_batches_if_enabled(Cfg, max_clips=1)

        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "max_clips_manual_run")

    def test_ffmpeg_worker_starts_next_video_before_export_packaging_finishes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            input_dir = temp_path / "input"
            input_dir.mkdir()
            first_video = input_dir / "a.mp4"
            second_video = input_dir / "b.mp4"
            first_video.write_bytes(b"")
            second_video.write_bytes(b"")

            runner = VideoQueueRunner(
                input_dir=str(input_dir),
                state_path=str(temp_path / "state.json"),
                max_retries=0,
                max_inflight_videos=1,
                max_clips=3,
                poll_interval=0.5,
                control_path=str(temp_path / "control.json"),
            )
            runner._sync_videos([first_video, second_video])
            first_key = str(first_video.resolve())
            second_key = str(second_video.resolve())

            with runner.state_lock:
                runner._mark_queue_running_locked()
                for video_key in (first_key, second_key):
                    entry = runner.state["videos"][video_key]
                    entry["status"] = "queued"
                    for stage in PRE_EDIT_STAGES:
                        entry["stages"][stage]["status"] = "done"
                    entry["stages"][EDIT_STAGE]["status"] = "pending"
                runner._schedule_locked("unit-test")

            started_videos = []
            started_lock = threading.Lock()
            second_started = threading.Event()
            packaging_started = threading.Event()
            release_packaging = threading.Event()
            first_packaging_finished = threading.Event()
            packaging_call_count = 0
            packaging_call_lock = threading.Lock()

            def fake_run_pipeline(**kwargs):
                with started_lock:
                    started_videos.append(Path(kwargs["video_path"]).name)
                    if len(started_videos) == 2:
                        second_started.set()
                self.assertEqual(kwargs["max_clips"], 3)
                import main

                class Cfg:
                    EXPORT_BATCHES_ENABLED = True
                    EXPORT_BATCH_SIZE = 15
                    EXPORT_BATCH_TIMEOUT_SECONDS = 30
                    OUTPUT_DIR = str(temp_path / "output")

                main._package_export_batches_if_enabled(Cfg)

            fake_export_packager = types.ModuleType("export_packager")

            def fake_package_export_batches(output_root, cfg=None, batch_size=None, trigger="direct"):
                self.assertEqual(trigger, "automatic")
                nonlocal packaging_call_count
                with packaging_call_lock:
                    packaging_call_count += 1
                    call_number = packaging_call_count
                packaging_started.set()
                if call_number == 1:
                    self.assertTrue(second_started.wait(timeout=2.0))
                    self.assertTrue(release_packaging.wait(timeout=5.0))
                    first_packaging_finished.set()
                return {
                    "eligible_count": 3,
                    "new_unique_count": 3,
                    "packaged_count": 3,
                    "duplicate_existing_count": 0,
                    "duplicate_candidate_count": 0,
                    "error_count": 0,
                    "assignments": [{"batch_folder": "1"}],
                }

            fake_export_packager.package_export_batches = fake_package_export_batches

            try:
                with mock.patch.dict(sys.modules, {"export_packager": fake_export_packager}), \
                        mock.patch("main.run_pipeline", side_effect=fake_run_pipeline):
                    runner._start_workers()
                    self.assertTrue(packaging_started.wait(timeout=2.0))
                    self.assertTrue(second_started.wait(timeout=2.0))
                    self.assertFalse(first_packaging_finished.is_set())
                    release_packaging.set()

                    deadline = time.time() + 5.0
                    while time.time() < deadline:
                        with runner.state_lock:
                            states = [
                                runner.state["videos"][first_key]["status"],
                                runner.state["videos"][second_key]["status"],
                            ]
                        if states == ["completed", "completed"]:
                            break
                        time.sleep(0.05)

                    self.assertTrue(first_packaging_finished.wait(timeout=2.0))
                    with runner.state_lock:
                        self.assertEqual(runner.state["videos"][first_key]["status"], "completed")
                        self.assertEqual(runner.state["videos"][second_key]["status"], "completed")
                    self.assertEqual(started_videos, ["a.mp4", "b.mp4"])
            finally:
                release_packaging.set()
                runner._stop_workers()

    def test_run_job_clears_queue_markers_when_stage_starts_and_finishes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            input_dir = temp_path / "input"
            input_dir.mkdir()
            video = input_dir / "a.mp4"
            video.write_bytes(b"")

            runner = VideoQueueRunner(
                input_dir=str(input_dir),
                state_path=str(temp_path / "state.json"),
                max_retries=0,
                max_inflight_videos=1,
                poll_interval=0.5,
                control_path=str(temp_path / "control.json"),
            )
            runner._sync_videos([video])
            runner._execute_stage = lambda _job: None
            video_key = str(video.resolve())

            with runner.state_lock:
                entry = runner.state["videos"][video_key]
                entry["status"] = "queued"
                entry["stages"]["transcribe"]["status"] = "queued"
                entry["stages"]["transcribe"]["queued"] = True
                entry["stages"]["transcribe"]["queued_at"] = "2026-05-27T16:42:06+07:00"

            runner._run_job("gpu-worker", StageJob(video_path=video_key, stage="transcribe"))

            with runner.state_lock:
                stage_state = runner.state["videos"][video_key]["stages"]["transcribe"]
                self.assertEqual(stage_state["status"], "done")
                self.assertFalse(stage_state["queued"])
                self.assertIsNone(stage_state["queued_at"])
                self.assertIsNone(runner.state["videos"][video_key]["current_stage"])

    def test_retry_failure_requeues_stage_without_failed_or_finished_markers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            input_dir = temp_path / "input"
            input_dir.mkdir()
            video = input_dir / "a.mp4"
            video.write_bytes(b"")

            runner = VideoQueueRunner(
                input_dir=str(input_dir),
                state_path=str(temp_path / "state.json"),
                max_retries=1,
                max_inflight_videos=1,
                poll_interval=0.5,
                control_path=str(temp_path / "control.json"),
            )
            runner._sync_videos([video])

            def fail_stage(_job):
                raise RuntimeError("boom")

            runner._execute_stage = fail_stage
            video_key = str(video.resolve())

            with runner.state_lock:
                entry = runner.state["videos"][video_key]
                entry["status"] = "queued"
                entry["stages"]["transcribe"]["status"] = "queued"
                entry["stages"]["transcribe"]["queued"] = True
                entry["stages"]["transcribe"]["queued_at"] = "2026-05-27T16:42:06+07:00"

            runner._run_job("gpu-worker", StageJob(video_path=video_key, stage="transcribe"))

            with runner.state_lock:
                entry = runner.state["videos"][video_key]
                stage_state = entry["stages"]["transcribe"]
                self.assertEqual(entry["status"], "queued")
                self.assertEqual(stage_state["status"], "queued")
                self.assertTrue(stage_state["queued"])
                self.assertIsNotNone(stage_state["queued_at"])
                self.assertIsNone(stage_state["finished_at"])
                self.assertIsNone(stage_state["duration_sec"])
                self.assertIn("RuntimeError: boom", stage_state["last_error"])
                self.assertIsNone(entry["current_stage"])
                self.assertEqual(runner.queues["gpu"].qsize(), 1)

    def test_retry_failed_resets_failed_stage_and_downstream_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            input_dir = temp_path / "input"
            input_dir.mkdir()
            video = input_dir / "a.mp4"
            video.write_bytes(b"")

            runner = VideoQueueRunner(
                input_dir=str(input_dir),
                state_path=str(temp_path / "state.json"),
                max_retries=0,
                max_inflight_videos=1,
                poll_interval=0.5,
            )
            runner._sync_videos([video])

            video_key = str(video.resolve())
            with runner.state_lock:
                entry = runner.state["videos"][video_key]
                entry["status"] = "failed"
                entry["failed_at"] = "2026-05-15T17:10:21+07:00"
                entry["stages"]["transcribe"]["status"] = "done"
                entry["stages"]["llm"]["status"] = "done"
                entry["stages"]["yolo"]["status"] = "failed"
                entry["stages"]["yolo"]["attempts"] = 3
                entry["stages"]["yolo"]["last_error"] = "AcceleratorError: CUDA error: unknown error"
                entry["stages"][EDIT_STAGE]["status"] = "pending"

                reset_count = runner._reset_failed_active_videos_locked()

                self.assertEqual(reset_count, 1)
                self.assertEqual(entry["status"], "waiting")
                self.assertIsNone(entry["failed_at"])
                self.assertEqual(entry["stages"]["transcribe"]["status"], "done")
                self.assertEqual(entry["stages"]["llm"]["status"], "done")
                self.assertEqual(entry["stages"]["yolo"]["status"], "pending")
                self.assertEqual(entry["stages"]["yolo"]["attempts"], 0)
                self.assertIsNone(entry["stages"]["yolo"]["last_error"])
                self.assertEqual(entry["stages"][EDIT_STAGE]["status"], "pending")

    def test_rescan_adds_new_stable_video_to_current_run_tag(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            input_dir = temp_path / "input"
            input_dir.mkdir()
            first_video = input_dir / "a.mp4"
            second_video = input_dir / "b.mp4"
            first_video.write_bytes(b"")

            runner = VideoQueueRunner(
                input_dir=str(input_dir),
                state_path=str(temp_path / "state.json"),
                max_retries=0,
                max_inflight_videos=1,
                poll_interval=0.5,
                scan_interval=0.5,
                stable_seconds=0,
                output_tag="_run_012",
                working_tag="_run_012",
            )
            runner._sync_videos(runner._discover_videos())
            second_video.write_bytes(b"")
            runner._sync_videos(runner._discover_videos())

            second_key = str(second_video.resolve())
            self.assertIn(second_key, runner.state["videos"])
            self.assertEqual(runner.state["videos"][second_key]["output_tag"], "_run_012")
            self.assertEqual(runner.state["videos"][second_key]["working_tag"], "_run_012")

    def test_rescan_does_not_duplicate_queued_ffmpeg_job(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            input_dir = temp_path / "input"
            input_dir.mkdir()
            video = input_dir / "a.mp4"
            video.write_bytes(b"")

            runner = VideoQueueRunner(
                input_dir=str(input_dir),
                state_path=str(temp_path / "state.json"),
                max_retries=0,
                max_inflight_videos=1,
                poll_interval=0.5,
                stable_seconds=0,
                output_tag="_run_012",
                working_tag="_run_012",
                control_path=str(temp_path / "control.json"),
            )
            runner._sync_videos(runner._discover_videos())
            video_key = str(video.resolve())

            with runner.state_lock:
                entry = runner.state["videos"][video_key]
                entry["status"] = "queued"
                for stage in PRE_EDIT_STAGES:
                    entry["stages"][stage]["status"] = "done"
                entry["stages"][EDIT_STAGE]["status"] = "pending"
                runner._schedule_locked("bootstrap")
                self.assertEqual(runner.queues["ffmpeg"].qsize(), 1)
                self.assertEqual(entry["stages"][EDIT_STAGE]["status"], "queued")

            runner._sync_videos(
                runner._discover_videos(),
                refresh_existing_from_disk=False,
            )
            with runner.state_lock:
                runner._schedule_locked("rescan")
                entry = runner.state["videos"][video_key]
                self.assertEqual(runner.queues["ffmpeg"].qsize(), 1)
                self.assertEqual(entry["stages"][EDIT_STAGE]["status"], "queued")
                self.assertTrue(entry["stages"][EDIT_STAGE]["queued"])

    def test_pause_request_prevents_new_stage_enqueue(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            input_dir = temp_path / "input"
            input_dir.mkdir()
            control_path = temp_path / "control.json"
            video = input_dir / "a.mp4"
            video.write_bytes(b"")

            runner = VideoQueueRunner(
                input_dir=str(input_dir),
                state_path=str(temp_path / "state.json"),
                max_retries=0,
                max_inflight_videos=1,
                poll_interval=0.5,
                stable_seconds=0,
                control_path=str(control_path),
            )
            runner._sync_videos([video])
            queue_control.request_stop(control_path)

            with runner.state_lock:
                runner._schedule_locked("unit-test")
                entry = runner.state["videos"][str(video.resolve())]
                self.assertEqual(entry["stages"]["transcribe"]["status"], "pending")
                self.assertFalse(entry["stages"]["transcribe"]["queued"])

    def test_running_queue_clears_stale_paused_at(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            input_dir = temp_path / "input"
            input_dir.mkdir()

            runner = VideoQueueRunner(
                input_dir=str(input_dir),
                state_path=str(temp_path / "state.json"),
                max_retries=0,
                max_inflight_videos=1,
                poll_interval=0.5,
            )

            with runner.state_lock:
                runner.state["queue_status"] = "paused"
                runner.state["paused_at"] = "2026-05-25T00:50:19+07:00"
                runner._mark_queue_running_locked()
                runner._save_state_locked()

                self.assertEqual(runner.state["queue_status"], "running")
                self.assertNotIn("paused_at", runner.state)
                persisted = json.loads((temp_path / "state.json").read_text(encoding="utf-8"))
                self.assertNotIn("paused_at", persisted)

    def test_enqueue_records_queued_at_for_dashboard_stall_detection(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            input_dir = temp_path / "input"
            input_dir.mkdir()
            video = input_dir / "a.mp4"
            video.write_bytes(b"")

            runner = VideoQueueRunner(
                input_dir=str(input_dir),
                state_path=str(temp_path / "state.json"),
                max_retries=0,
                max_inflight_videos=1,
                poll_interval=0.5,
            )
            runner._sync_videos([video])
            runner._stage_output_current = lambda _entry, stage, _path: stage in PRE_EDIT_STAGES
            video_key = str(video.resolve())

            with runner.state_lock:
                entry = runner.state["videos"][video_key]
                for stage in PRE_EDIT_STAGES:
                    entry["stages"][stage]["status"] = "done"

                runner._enqueue_stage_locked(video_key, EDIT_STAGE, reason="unit-test")

                stage_state = entry["stages"][EDIT_STAGE]
                self.assertEqual(stage_state["status"], "queued")
                self.assertTrue(stage_state["queued"])
                self.assertIsNotNone(stage_state["queued_at"])

    def test_interrupted_ffmpeg_refresh_clears_stale_current_stage_for_requeue(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            input_dir = temp_path / "input"
            input_dir.mkdir()
            video = input_dir / "a.mp4"
            video.write_bytes(b"")

            runner = VideoQueueRunner(
                input_dir=str(input_dir),
                state_path=str(temp_path / "state.json"),
                max_retries=0,
                max_inflight_videos=1,
                poll_interval=0.5,
                stable_seconds=0,
                output_tag="_run_001",
                working_tag="_run_001",
            )
            runner._sync_videos([video])
            runner._stage_output_current = lambda _entry, stage, _path: stage in PRE_EDIT_STAGES
            video_key = str(video.resolve())

            with runner.state_lock:
                entry = runner.state["videos"][video_key]
                for stage in PRE_EDIT_STAGES:
                    entry["stages"][stage]["status"] = "done"
                entry["status"] = "queued"
                entry["current_stage"] = EDIT_STAGE
                entry["stages"][EDIT_STAGE]["status"] = "running"
                entry["stages"][EDIT_STAGE]["queued"] = True
                entry["stages"][EDIT_STAGE]["queued_at"] = "2026-05-27T16:41:00+07:00"
                entry["stages"][EDIT_STAGE]["started_at"] = "2026-05-27T16:42:06+07:00"
                entry["stages"][EDIT_STAGE]["active_clip_renders"] = 2
                entry["stages"][EDIT_STAGE]["render_paused"] = True

                runner._refresh_stage_status_from_disk(entry)
                stage_state = entry["stages"][EDIT_STAGE]
                self.assertIsNone(entry["current_stage"])
                self.assertEqual(stage_state["status"], "pending")
                self.assertFalse(stage_state["queued"])
                self.assertIsNone(stage_state["queued_at"])
                self.assertEqual(stage_state["active_clip_renders"], 0)
                self.assertFalse(stage_state["render_paused"])

                runner._schedule_locked("restart")

                stage_state = entry["stages"][EDIT_STAGE]
                self.assertIsNone(entry["current_stage"])
                self.assertEqual(stage_state["status"], "queued")
                self.assertTrue(stage_state["queued"])
                self.assertEqual(runner.queues["ffmpeg"].qsize(), 1)

    def test_paused_ffmpeg_resume_enqueues_as_queued_not_paused(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            input_dir = temp_path / "input"
            input_dir.mkdir()
            video = input_dir / "a.mp4"
            video.write_bytes(b"")

            runner = VideoQueueRunner(
                input_dir=str(input_dir),
                state_path=str(temp_path / "state.json"),
                max_retries=0,
                max_inflight_videos=1,
                poll_interval=0.5,
                stable_seconds=0,
                output_tag="_run_001",
                working_tag="_run_001",
                control_path=str(temp_path / "control.json"),
            )
            runner._sync_videos([video])
            video_key = str(video.resolve())

            with runner.state_lock:
                runner.state["queue_status"] = "running"
                entry = runner.state["videos"][video_key]
                for stage in PRE_EDIT_STAGES:
                    entry["stages"][stage]["status"] = "done"
                entry["status"] = "paused"
                entry["current_stage"] = None
                entry["stages"][EDIT_STAGE]["status"] = "paused"
                entry["stages"][EDIT_STAGE]["queued"] = False
                entry["stages"][EDIT_STAGE]["queued_at"] = None
                entry["stages"][EDIT_STAGE]["render_paused"] = True

                runner._schedule_locked("resume")

                stage_state = entry["stages"][EDIT_STAGE]
                self.assertEqual(entry["status"], "queued")
                self.assertEqual(stage_state["status"], "queued")
                self.assertTrue(stage_state["queued"])
                self.assertIsNotNone(stage_state["queued_at"])
                self.assertFalse(stage_state["render_paused"])
                self.assertEqual(runner.queues["ffmpeg"].qsize(), 1)

    def test_refresh_preserves_paused_video_while_queue_is_paused(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            input_dir = temp_path / "input"
            input_dir.mkdir()
            control_path = temp_path / "control.json"
            video = input_dir / "a.mp4"
            video.write_bytes(b"")

            runner = VideoQueueRunner(
                input_dir=str(input_dir),
                state_path=str(temp_path / "state.json"),
                max_retries=0,
                max_inflight_videos=1,
                poll_interval=0.5,
                stable_seconds=0,
                output_tag="_run_001",
                working_tag="_run_001",
                control_path=str(control_path),
            )
            runner._sync_videos([video])
            queue_control.request_stop(control_path)
            video_key = str(video.resolve())

            with runner.state_lock:
                runner.state["queue_status"] = "paused"
                entry = runner.state["videos"][video_key]
                for stage in PRE_EDIT_STAGES:
                    entry["stages"][stage]["status"] = "done"
                entry["status"] = "paused"
                entry["current_stage"] = EDIT_STAGE
                entry["stages"][EDIT_STAGE]["status"] = "paused"
                entry["stages"][EDIT_STAGE]["queued"] = True
                entry["stages"][EDIT_STAGE]["queued_at"] = "2026-05-27T16:41:00+07:00"

                runner._refresh_stage_status_from_disk(entry)

                stage_state = entry["stages"][EDIT_STAGE]
                self.assertEqual(entry["status"], "paused")
                self.assertIsNone(entry["current_stage"])
                self.assertEqual(stage_state["status"], "paused")
                self.assertFalse(stage_state["queued"])
                self.assertIsNone(stage_state["queued_at"])

    def test_pending_ffmpeg_with_stale_current_stage_requeues_on_restart(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            input_dir = temp_path / "input"
            input_dir.mkdir()
            video = input_dir / "a.mp4"
            video.write_bytes(b"")

            runner = VideoQueueRunner(
                input_dir=str(input_dir),
                state_path=str(temp_path / "state.json"),
                max_retries=0,
                max_inflight_videos=1,
                poll_interval=0.5,
                stable_seconds=0,
                output_tag="_run_001",
                working_tag="_run_001",
            )
            runner._sync_videos([video])
            runner._stage_output_current = lambda _entry, stage, _path: stage in PRE_EDIT_STAGES
            video_key = str(video.resolve())

            with runner.state_lock:
                entry = runner.state["videos"][video_key]
                for stage in PRE_EDIT_STAGES:
                    entry["stages"][stage]["status"] = "done"
                entry["status"] = "queued"
                entry["current_stage"] = EDIT_STAGE
                entry["stages"][EDIT_STAGE]["status"] = "pending"
                entry["stages"][EDIT_STAGE]["queued"] = False
                entry["stages"][EDIT_STAGE]["attempts"] = 1
                entry["stages"][EDIT_STAGE]["started_at"] = "2026-05-27T16:42:06+07:00"
                entry["stages"][EDIT_STAGE]["last_progress_at"] = "2026-05-27T16:46:54+07:00"

                runner._refresh_stage_status_from_disk(entry)
                runner._schedule_locked("restart")

                stage_state = entry["stages"][EDIT_STAGE]
                self.assertIsNone(entry["current_stage"])
                self.assertEqual(stage_state["status"], "queued")
                self.assertTrue(stage_state["queued"])
                self.assertEqual(runner.queues["ffmpeg"].qsize(), 1)


if __name__ == "__main__":
    unittest.main()
