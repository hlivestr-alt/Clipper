import json
import tempfile
import unittest
from pathlib import Path

import queue_control
from video_queue import (
    EDIT_STAGE,
    PRE_EDIT_STAGES,
    STAGES,
    StageJob,
    VideoQueueRunner,
    _build_versioned_stem,
    _reuse_base_transcript_for_tagged_run,
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
                self.assertEqual(entry["status"], "queued")
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


if __name__ == "__main__":
    unittest.main()
