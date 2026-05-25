import json
import os
import tempfile
import time
import unittest
from pathlib import Path

import queue_control
from queue_supervisor import format_run_tag, queue_run_terminal


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


if __name__ == "__main__":
    unittest.main()
