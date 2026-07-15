from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from clipper_app.application.catalog import (
    CatalogDatabase,
    CatalogIndexer,
    ChangeEventRepository,
)
from clipper_app.application.read_services import ReadDashboardService
from clipper_app.application.settings import LegacyConfigProvider


class CatalogDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = CatalogDatabase(self.root / "working" / "catalog" / "clipper.sqlite3")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_schema_status_and_durable_event_replay(self) -> None:
        events = ChangeEventRepository(self.database)
        first = events.publish(("queue", "jobs"))
        second = events.publish(("queue",))

        reset, replay = events.after(first.event_id)

        self.assertFalse(reset)
        self.assertEqual([event.event_id for event in replay], [second.event_id])
        self.assertEqual(replay[0].revisions["queue"], 2)
        status = self.database.status()
        self.assertEqual(status["integrity"], "ok")
        self.assertEqual(status["table_counts"]["change_events"], 2)

        self.database.reset_instance()
        reset, replay = events.after(second.event_id)
        self.assertTrue(reset)
        self.assertEqual(replay, [])

    def test_backfill_materializes_query_rows(self) -> None:
        output = self.root / "output"
        modules = self.root / "modules"
        working = self.root / "working"
        output.mkdir()
        modules.mkdir()
        working.mkdir(exist_ok=True)
        run = output / "vod_run_001"
        run.mkdir()
        (run / "manifest.json").write_text(
            json.dumps([{"clip_id": "clip_1", "product": "serum", "status": "ok", "output_file": "clip.mp4"}]),
            encoding="utf-8",
        )
        (run / "scores_summary.json").write_text(
            json.dumps({"schema_version": 2, "updated_at": "2026-01-01T00:00:00+00:00", "groups": []}),
            encoding="utf-8",
        )
        (modules / "index.json").write_text(
            json.dumps({"schema_version": 2, "modules": [{"module_id": "m1", "product": "serum", "role": "hook"}]}),
            encoding="utf-8",
        )
        state = working / "state.json"
        state.write_text(json.dumps({"schema_version": 2, "videos": {}}), encoding="utf-8")
        cfg = SimpleNamespace(
            OUTPUT_DIR=str(output),
            MODULE_LIBRARY_DIR=str(modules),
            WORKING_DIR=str(working),
            QUEUE_STATE_FILE=str(state),
            QUEUE_CONTROL_FILE=str(working / "control.json"),
            QUEUE_FOREVER_STATE_FILE=str(working / "forever.json"),
        )

        result = CatalogIndexer(self.database, cfg).backfill()

        self.assertEqual(result["errors"], 0)
        self.assertEqual(self.database.scalar("SELECT COUNT(*) FROM clips"), 1)
        self.assertEqual(self.database.scalar("SELECT COUNT(*) FROM modules"), 1)
        self.assertTrue(CatalogIndexer(self.database, cfg).verify()["ok"])

        with mock.patch.dict(os.environ, {"CLIPPER_CATALOG_MODE": "catalog", "CLIPPER_CATALOG_PATH": str(self.database.path)}):
            reads = ReadDashboardService(LegacyConfigProvider(cfg))
            with mock.patch.object(reads, "_module_corpus", side_effect=AssertionError("request-time crawl")):
                page = reads.module_library(limit=20)
            self.assertEqual(page.data.total, 1)
            self.assertEqual(page.data.rows[0].module_id, "m1")
            self.assertEqual(reads.scores(limit=20).data.total, 0)


if __name__ == "__main__":
    unittest.main()
