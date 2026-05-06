import json
import tempfile
import unittest
from pathlib import Path

from video_queue import (
    EDIT_STAGE,
    PRE_EDIT_STAGES,
    STAGES,
    StageJob,
    VideoQueueRunner,
    _reuse_base_transcript_for_tagged_run,
)


class VideoQueueSchedulingTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
