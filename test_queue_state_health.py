import unittest
from datetime import datetime, timedelta, timezone

import queue_state_health as qh


class QueueStateHealthTests(unittest.TestCase):
    def test_clean_paused_queue_does_not_need_attention(self):
        now = datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc)
        state = {
            "queue_status": "paused",
            "paused_at": (now - timedelta(hours=1)).isoformat(timespec="seconds"),
            "videos": {
                "D:/VOD/a.mp4": {
                    "name": "a.mp4",
                    "path": "D:/VOD/a.mp4",
                    "status": "paused",
                    "current_stage": None,
                    "created_at": (now - timedelta(days=1)).isoformat(timespec="seconds"),
                    "stages": {
                        "transcribe": {"status": "done"},
                        "llm": {"status": "done"},
                        "yolo": {"status": "done"},
                        "ffmpeg": {
                            "status": "paused",
                            "queued": False,
                            "queued_at": None,
                            "active_clip_renders": 0,
                            "render_paused": True,
                        },
                    },
                }
            },
        }

        health = qh.derive_queue_health(state, now=now)

        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["status_label"], "Paused")
        self.assertEqual(health["issues"], [])

    def test_running_queue_with_stale_pause_and_ffmpeg_waiting_needs_attention(self):
        now = datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc)
        yolo_finished = now - timedelta(days=3)
        state = {
            "queue_status": "running",
            "paused_at": (now - timedelta(days=2)).isoformat(timespec="seconds"),
            "videos": {
                "D:/VOD/a.mp4": {
                    "name": "a.mp4",
                    "path": "D:/VOD/a.mp4",
                    "status": "queued",
                    "created_at": (now - timedelta(days=4)).isoformat(timespec="seconds"),
                    "stages": {
                        "transcribe": {"status": "done", "finished_at": (now - timedelta(days=4)).isoformat(timespec="seconds")},
                        "llm": {"status": "done", "finished_at": (now - timedelta(days=4)).isoformat(timespec="seconds")},
                        "yolo": {"status": "done", "finished_at": yolo_finished.isoformat(timespec="seconds")},
                        "ffmpeg": {"status": "queued", "queued": True},
                    },
                }
            },
        }

        health = qh.derive_queue_health(state, now=now)

        self.assertEqual(health["status"], "needs_attention")
        self.assertEqual(health["severity"], "warning")
        self.assertEqual(health["attention_video_count"], 1)
        self.assertIn("stale_paused_at", health["issue_counts"])
        self.assertIn("queued_stalled", health["issue_counts"])

    def test_stale_running_ffmpeg_is_critical(self):
        now = datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc)
        state = {
            "queue_status": "running",
            "videos": {
                "D:/VOD/a.mp4": {
                    "name": "a.mp4",
                    "path": "D:/VOD/a.mp4",
                    "status": "running",
                    "current_stage": "ffmpeg",
                    "created_at": (now - timedelta(days=1)).isoformat(timespec="seconds"),
                    "stages": {
                        "transcribe": {"status": "done"},
                        "llm": {"status": "done"},
                        "yolo": {"status": "done"},
                        "ffmpeg": {
                            "status": "running",
                            "last_progress_at": (now - timedelta(hours=3)).isoformat(timespec="seconds"),
                            "active_clip_renders": 0,
                        },
                    },
                }
            },
        }

        health = qh.derive_queue_health(state, now=now)

        self.assertEqual(health["status"], "needs_attention")
        self.assertEqual(health["severity"], "critical")
        self.assertEqual(health["status_label"], "Stalled")
        self.assertEqual(health["stalled_stage_count"], 1)

    def test_paused_queue_does_not_turn_ready_ffmpeg_into_stall_attention(self):
        now = datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc)
        state = {
            "queue_status": "paused",
            "paused_at": (now - timedelta(days=2)).isoformat(timespec="seconds"),
            "videos": {
                "D:/VOD/a.mp4": {
                    "name": "a.mp4",
                    "path": "D:/VOD/a.mp4",
                    "status": "queued",
                    "created_at": (now - timedelta(days=4)).isoformat(timespec="seconds"),
                    "stages": {
                        "transcribe": {"status": "done", "finished_at": (now - timedelta(days=4)).isoformat(timespec="seconds")},
                        "llm": {"status": "done", "finished_at": (now - timedelta(days=4)).isoformat(timespec="seconds")},
                        "yolo": {"status": "done", "finished_at": (now - timedelta(days=3)).isoformat(timespec="seconds")},
                        "ffmpeg": {"status": "pending", "queued": False, "queued_at": None},
                    },
                }
            },
        }

        health = qh.derive_queue_health(state, now=now)

        self.assertEqual(health["status"], "ok")
        self.assertNotIn("ready_not_enqueued", health["issue_counts"])

    def test_inactive_stage_with_queue_markers_is_attention_not_queue_stall(self):
        now = datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc)
        state = {
            "queue_status": "running",
            "videos": {
                "D:/VOD/a.mp4": {
                    "name": "a.mp4",
                    "path": "D:/VOD/a.mp4",
                    "status": "queued",
                    "created_at": (now - timedelta(days=1)).isoformat(timespec="seconds"),
                    "stages": {
                        "transcribe": {"status": "done"},
                        "llm": {"status": "done"},
                        "yolo": {"status": "done"},
                        "ffmpeg": {
                            "status": "paused",
                            "queued": True,
                            "queued_at": (now - timedelta(days=3)).isoformat(timespec="seconds"),
                        },
                    },
                }
            },
        }

        health = qh.derive_queue_health(state, now=now)

        self.assertEqual(health["status"], "needs_attention")
        self.assertIn("inactive_stage_queue_marker", health["issue_counts"])
        self.assertNotIn("queued_stalled", health["issue_counts"])
        self.assertEqual(health["stale_queue_marker_count"], 1)

    def test_completed_video_with_stale_queued_flag_needs_marker_attention(self):
        now = datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc)
        state = {
            "queue_status": "completed",
            "videos": {
                "D:/VOD/a.mp4": {
                    "name": "a.mp4",
                    "path": "D:/VOD/a.mp4",
                    "status": "completed",
                    "stages": {
                        "transcribe": {"status": "done"},
                        "llm": {"status": "done"},
                        "yolo": {"status": "done"},
                        "ffmpeg": {
                            "status": "done",
                            "queued": True,
                            "queued_at": (now - timedelta(hours=1)).isoformat(timespec="seconds"),
                        },
                    },
                }
            },
        }

        health = qh.derive_queue_health(state, now=now)

        self.assertEqual(health["status"], "needs_attention")
        self.assertIn("completed_with_queue_marker", health["issue_counts"])
        self.assertEqual(health["stale_queue_marker_count"], 1)

    def test_failed_stage_that_is_still_queued_is_critical(self):
        now = datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc)
        state = {
            "queue_status": "running",
            "videos": {
                "D:/VOD/a.mp4": {
                    "name": "a.mp4",
                    "path": "D:/VOD/a.mp4",
                    "status": "queued",
                    "stages": {
                        "transcribe": {"status": "done"},
                        "llm": {"status": "done"},
                        "yolo": {"status": "done"},
                        "ffmpeg": {
                            "status": "failed",
                            "queued": True,
                            "finished_at": (now - timedelta(hours=5)).isoformat(timespec="seconds"),
                        },
                    },
                }
            },
        }

        health = qh.derive_queue_health(state, now=now)

        self.assertEqual(health["severity"], "critical")
        self.assertEqual(health["failed_stage_count"], 1)
        self.assertIn("failed_stage_nonterminal", health["issue_counts"])


if __name__ == "__main__":
    unittest.main()
