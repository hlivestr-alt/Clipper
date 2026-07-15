from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from clipper_app.application.catalog import CatalogDatabase
from clipper_app.application.queue_repository import QueueStateRepository


def sample_state() -> dict:
    run = {
        "working_dir": "working/vod_run_001",
        "output_dir": "output/vod_run_001",
        "status": "completed",
        "created_at": "2026-01-01T00:00:00+00:00",
        "archived_at": "2026-01-01T01:00:00+00:00",
        "stages": {"ffmpeg": {"status": "done", "clips_created": 2}},
    }
    return {
        "schema_version": 2,
        "queue_status": "running",
        "updated_at": "2026-01-02T00:00:00+00:00",
        "videos": {
            "C:/vod.mp4": {
                "name": "vod.mp4",
                "status": "waiting",
                "stages": {"transcribe": {"status": "pending"}},
                "run_history": [run],
            }
        },
    }


class QueueStateRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state_path = self.root / "working" / "video_queue_state.json"
        self.database = CatalogDatabase(self.root / "working" / "catalog" / "clipper.sqlite3")
        self.repository = QueueStateRepository(self.state_path, database=self.database, mode="sqlite")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_sqlite_state_snapshot_and_journal_round_trip(self) -> None:
        state = sample_state()
        self.repository.save(state, lifecycle=True)

        compatibility = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(compatibility["schema_version"], 3)
        self.assertNotIn("run_history", compatibility["videos"]["C:/vod.mp4"])

        loaded = self.repository.load()
        self.assertEqual(loaded["videos"]["C:/vod.mp4"]["run_history"][0]["status"], "completed")
        self.assertEqual(self.repository.status()["history_runs"], 1)
        journal = self.root / "working" / "queue_history" / "2026-01.jsonl"
        record = json.loads(journal.read_text(encoding="utf-8").strip())
        self.assertEqual(record["checksum"], self.database.scalar("SELECT checksum FROM queue_run_history"))

        self.repository.save(state, lifecycle=True)
        self.assertEqual(self.repository.status()["history_runs"], 1)
        self.assertEqual(len(journal.read_text(encoding="utf-8").splitlines()), 1)

    def test_history_rows_are_immutable(self) -> None:
        self.repository.save(sample_state(), lifecycle=True)
        connection = self.database.connect()
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE queue_run_history SET checksum='changed'")
        finally:
            connection.close()

    def test_legacy_export_restores_embedded_history(self) -> None:
        self.repository.import_legacy(sample_state())
        destination = self.root / "exported-v2.json"
        self.repository.export_legacy(destination)
        exported = json.loads(destination.read_text(encoding="utf-8"))
        self.assertEqual(exported["schema_version"], 2)
        self.assertEqual(len(exported["videos"]["C:/vod.mp4"]["run_history"]), 1)

    def test_journal_orphan_is_recovered_into_a_rebuilt_database(self) -> None:
        self.repository.save(sample_state(), lifecycle=True)
        rebuilt = QueueStateRepository(
            self.state_path,
            database=CatalogDatabase(self.root / "working" / "catalog" / "rebuilt.sqlite3"),
            mode="sqlite",
        )

        loaded = rebuilt.load()

        self.assertEqual(rebuilt.status()["history_runs"], 1)
        self.assertEqual(len(loaded["videos"]["C:/vod.mp4"]["run_history"]), 1)
        self.assertTrue(rebuilt.verify_history()["ok"])

    def test_progress_writes_coalesce_but_lifecycle_writes_flush(self) -> None:
        state = sample_state()
        state["videos"]["C:/vod.mp4"]["run_history"] = []
        self.repository.save(state)
        state["videos"]["C:/vod.mp4"]["status"] = "running"
        self.repository.save(state)
        self.assertEqual(self.repository.load()["videos"]["C:/vod.mp4"]["status"], "waiting")

        self.repository.save(state, lifecycle=True)
        self.assertEqual(self.repository.load()["videos"]["C:/vod.mp4"]["status"], "running")


if __name__ == "__main__":
    unittest.main()
