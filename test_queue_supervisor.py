import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

import queue_control
from queue_supervisor import build_queue_command, format_run_tag, queue_run_terminal, run_supervisor


class QueueSupervisorTerminalTests(unittest.TestCase):
    def _write_video(self, input_dir: Path, name: str) -> Path:
        path = input_dir / name
        path.write_bytes(b"video")
        old = time.time() - 120
        os.utime(path, (old, old))
        return path

    def _write_state(self, state_path: Path, videos: dict) -> None:
        state_path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "queue_status": "completed",
                    "videos": videos,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def _entry(self, video: Path, run_tag: str, status: str, ffmpeg_status: str = "done", active: int = 0):
        return {
            "name": video.name,
            "path": str(video.resolve()),
            "working_tag": run_tag,
            "output_tag": run_tag,
            "status": status,
            "stages": {
                "transcribe": {"status": "done"},
                "llm": {"status": "done"},
                "yolo": {"status": "done" if status == "completed" else "failed"},
                "ffmpeg": {"status": ffmpeg_status, "active_clip_renders": active},
            },
        }

    def test_run_terminal_requires_all_videos_terminal_no_pending_and_no_active_renders(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "vod"
            input_dir.mkdir()
            state_path = root / "state.json"
            run_tag = format_run_tag(12)
            first = self._write_video(input_dir, "a.mp4")
            second = self._write_video(input_dir, "b.mp4")

            self._write_state(
                state_path,
                {
                    str(first.resolve()): self._entry(first, run_tag, "completed"),
                    str(second.resolve()): self._entry(second, run_tag, "failed", ffmpeg_status="skipped"),
                },
            )

            summary = queue_run_terminal(state_path, input_dir, run_tag, stable_seconds=0)

            self.assertTrue(summary.is_terminal)
            self.assertEqual(summary.completed, 1)
            self.assertEqual(summary.failed, 1)

    def test_paused_run_is_not_terminal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "vod"
            input_dir.mkdir()
            state_path = root / "state.json"
            run_tag = format_run_tag(12)
            video = self._write_video(input_dir, "a.mp4")
            entry = self._entry(video, run_tag, queue_control.PAUSED_STATUS, ffmpeg_status="paused")
            self._write_state(state_path, {str(video.resolve()): entry})

            summary = queue_run_terminal(state_path, input_dir, run_tag, stable_seconds=0)

            self.assertFalse(summary.is_terminal)
            self.assertTrue(summary.paused)

    def test_active_clip_render_prevents_terminal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "vod"
            input_dir.mkdir()
            state_path = root / "state.json"
            run_tag = format_run_tag(12)
            video = self._write_video(input_dir, "a.mp4")
            entry = self._entry(video, run_tag, "completed", active=1)
            self._write_state(state_path, {str(video.resolve()): entry})

            summary = queue_run_terminal(state_path, input_dir, run_tag, stable_seconds=0)

            self.assertFalse(summary.is_terminal)
            self.assertEqual(summary.active_clip_renders, 1)

    def test_pending_stage_prevents_terminal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "vod"
            input_dir.mkdir()
            state_path = root / "state.json"
            run_tag = format_run_tag(12)
            video = self._write_video(input_dir, "a.mp4")
            entry = self._entry(video, run_tag, "completed", ffmpeg_status="pending")
            self._write_state(state_path, {str(video.resolve()): entry})

            summary = queue_run_terminal(state_path, input_dir, run_tag, stable_seconds=0)

            self.assertFalse(summary.is_terminal)
            self.assertEqual(summary.pending_stages, 1)

    def test_queue_command_passes_launcher_modes_and_scan_once(self):
        args = type(
            "Args",
            (),
            {
                "python_exe": "python",
                "input_dir": "vod",
                "state_file": "state.json",
                "max_retries": 2,
                "max_inflight_videos": 1,
                "ffmpeg_max_parallel_clips": 2,
                "stage_admission_limit": 3,
                "poll_interval": 2.0,
                "control_file": "control.json",
                "stable_seconds": 0.0,
                "scan_interval": 10.0,
                "max_clips": None,
                "min_score": None,
                "force_rescore": False,
                "force_modules": False,
                "retry_failed": False,
                "run_mode": "folder_once",
                "pipeline_mode": "clips_only",
                "variant_mode": "custom",
                "variant_count": 3,
                "video_path": None,
                "settings_snapshot_file": "settings_snapshot.json",
            },
        )()

        command = build_queue_command(args, "_run_001")

        self.assertIn("--scan-once", command)
        self.assertIn("--run-mode", command)
        self.assertIn("folder_once", command)
        self.assertIn("--pipeline-mode", command)
        self.assertIn("clips_only", command)
        self.assertIn("--variant-mode", command)
        self.assertIn("custom", command)
        self.assertIn("--variant-count", command)
        self.assertIn("3", command)
        self.assertIn("--stage-admission-limit", command)
        self.assertIn("--settings-snapshot-file", command)
        self.assertIn("settings_snapshot.json", command)

    def test_one_shot_start_advances_past_completed_previous_run(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "vod"
            input_dir.mkdir()
            state_path = root / "state.json"
            forever_path = root / "forever.json"
            control_path = root / "control.json"
            video = self._write_video(input_dir, "a.mp4")
            previous_tag = format_run_tag(145)

            self._write_state(
                state_path,
                {str(video.resolve()): self._entry(video, previous_tag, "completed")},
            )
            forever_path.write_text(
                json.dumps(
                    {
                        "current_run_number": 145,
                        "current_run_tag": previous_tag,
                        "status": "completed",
                        "queue_summary": {"is_terminal": True},
                    }
                ),
                encoding="utf-8",
            )
            queue_control.request_start(
                control_path,
                {
                    "run_mode": "single_video",
                    "pipeline_mode": "full",
                    "variant_mode": "all",
                    "variant_count": 1,
                    "max_clips": 2,
                    "video_path": str(video.resolve()),
                },
            )
            args = type(
                "Args",
                (),
                {
                    "python_exe": "python",
                    "input_dir": str(input_dir),
                    "state_file": str(state_path),
                    "forever_state_file": str(forever_path),
                    "control_file": str(control_path),
                    "start_run_number": 1,
                    "max_retries": 2,
                    "max_inflight_videos": 1,
                    "ffmpeg_max_parallel_clips": 2,
                    "poll_interval": 2.0,
                    "stable_seconds": 0.0,
                    "scan_interval": 10.0,
                    "restart_delay_seconds": 0.0,
                    "between_runs_delay_seconds": 0.0,
                    "max_clips": 2,
                    "min_score": None,
                    "force_rescore": False,
                    "force_modules": False,
                    "retry_failed": False,
                    "dry_run": True,
                    "run_mode": "single_video",
                    "pipeline_mode": "full",
                    "variant_mode": "all",
                    "variant_count": 1,
                    "video_path": str(video.resolve()),
                },
            )()

            output = StringIO()
            with redirect_stdout(output):
                exit_code = run_supervisor(args)

            self.assertEqual(exit_code, 0)
            text = output.getvalue()
            self.assertIn("--redo-tag _run_146", text)
            self.assertNotIn("Run _run_145 is terminal", text)


if __name__ == "__main__":
    unittest.main()
