from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from clipper_app.modular_scanner.constants import ANALYZER_VERSION, PROMPT_VERSION
from clipper_app.modular_scanner.media import source_record
from clipper_app.modular_scanner.repository import ScannerRepository
from clipper_app.modular_scanner.service import ModularScannerService
from clipper_app.modular_scanner.transcripts import transcript_fingerprint, write_transcript_atomic


class ModularScannerBatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.vods = self.root / "vods"
        self.working = self.root / "working"
        self.vods.mkdir()
        self.working.mkdir()
        self.cfg = SimpleNamespace(
            WORKING_DIR=str(self.working), QUEUE_INPUT_DIR=str(self.vods), MODSCAN_ENABLED=True,
            LM_STUDIO_MOMENT_MODEL_ID="exact/model",
        )
        self.repository = ScannerRepository(self.working / "modular_library.sqlite3")
        self.records: list[dict] = []
        self.transcripts: dict[str, dict] = {}
        for name in ("active.mp4", "current.mp4", "stale.mp4", "unscanned.mp4"):
            path = self.vods / name
            path.write_bytes((name * 8).encode("utf-8"))
            record = source_record(path, include_duration=False)
            record["duration_seconds"] = 40.0
            record = self.repository.upsert_source(record)
            self.records.append(record)
            transcript = {"segments": [{"start": 0.0, "end": 20.0, "text": "serum vitamin c membantu kulit"}]}
            transcript_path = self.working / "modular_scanner" / "transcripts" / record["source_id"] / "transcript.json"
            write_transcript_atomic(transcript_path, transcript)
            self.transcripts[record["source_id"]] = self.repository.add_transcript(
                record["source_id"], "scanner", str(transcript_path), transcript_fingerprint(transcript),
            )
        by_name = {record["filename"]: record for record in self.records}
        current = self.repository.create_scan(
            by_name["current.mp4"]["source_id"], "scan", ANALYZER_VERSION, PROMPT_VERSION, "exact/model",
        )
        self.repository.update_scan(
            current["scan_id"], "completed",
            transcript_id=self.transcripts[current["source_id"]]["transcript_id"],
        )
        self.active = self.repository.create_scan(
            by_name["active.mp4"]["source_id"], "scan", ANALYZER_VERSION, PROMPT_VERSION, "exact/model",
        )
        stale = self.repository.create_scan(
            by_name["stale.mp4"]["source_id"], "scan", "modscan-old", PROMPT_VERSION, "exact/model",
        )
        self.repository.update_scan(
            stale["scan_id"], "completed",
            transcript_id=self.transcripts[stale["source_id"]]["transcript_id"],
        )
        self.service = ModularScannerService(
            self.cfg, repository=self.repository, analyzer_factory=lambda: mock.Mock(),
            production_active=lambda: False, start_worker=False,
        )

    def tearDown(self) -> None:
        self.service.close()
        self.temp.cleanup()

    def test_preview_launch_and_second_launch_are_compatible_and_idempotent(self) -> None:
        preview = self.service.batch_preview()
        self.assertEqual(
            {key: preview[key] for key in ("total_eligible", "already_current", "already_active", "would_queue")},
            {"total_eligible": 4, "already_current": 1, "already_active": 1, "would_queue": 2},
        )
        dispositions = {item["filename"]: item["disposition"] for item in preview["sources"]}
        self.assertEqual(dispositions["stale.mp4"], "would_queue")
        self.assertEqual(dispositions["unscanned.mp4"], "would_queue")

        launched = self.service.start_batch()
        self.assertTrue(launched["launched"])
        batch = launched["batch"]
        self.assertEqual(batch["status"], "preparing")
        self.assertEqual(batch["queued"], 0)
        generations = {
            record["source_id"]: len(self.repository.list_scans(record["source_id"])) for record in self.records
        }
        repeated = self.service.start_batch()
        self.assertTrue(repeated["launched"])
        self.assertTrue(repeated["reused"])
        self.assertEqual(repeated["batch"]["batch_id"], batch["batch_id"])
        self.assertEqual(generations, {
            record["source_id"]: len(self.repository.list_scans(record["source_id"])) for record in self.records
        })
        self.service.prepare_batch(batch["batch_id"])
        prepared = self.service.batch_status(batch["batch_id"])
        self.assertEqual((prepared["queued"], prepared["already_current"], prepared["already_active"]), (2, 1, 1))

    def test_batch_status_derives_completion_failure_and_restart_without_duplicates(self) -> None:
        launched = self.service.start_batch()
        batch_id = launched["batch"]["batch_id"]
        self.service.prepare_batch(batch_id)
        queued_items = [
            item for item in self.repository.batch_items(batch_id) if item["disposition"] == "queued"
        ]
        generations = {
            record["source_id"]: len(self.repository.list_scans(record["source_id"])) for record in self.records
        }
        restarted = ModularScannerService(
            self.cfg, repository=self.repository, analyzer_factory=lambda: mock.Mock(),
            production_active=lambda: False, start_worker=False,
        )
        try:
            self.assertEqual(restarted.batch_status(batch_id)["remaining"], 2)
            repeated = restarted.start_batch()
            self.assertTrue(repeated["reused"])
            self.assertEqual(repeated["batch"]["batch_id"], batch_id)
            self.assertEqual(generations, {
                record["source_id"]: len(self.repository.list_scans(record["source_id"])) for record in self.records
            })
        finally:
            restarted.close()
        self.repository.update_scan(queued_items[0]["scan_id"], "completed")
        self.repository.update_scan(queued_items[1]["scan_id"], "failed", error="analysis failed")
        status = self.service.batch_status(batch_id)
        self.assertEqual((status["completed"], status["failed"], status["remaining"]), (1, 1, 0))
        self.assertEqual(status["status"], "completed_with_failures")

    def test_zero_needed_preview_does_not_require_launch(self) -> None:
        for record in self.records:
            active = self.repository.active_scan(record["source_id"])
            if active is None:
                compatible = self.repository.compatible_scan(
                    record["source_id"], self.transcripts[record["source_id"]]["transcript_fingerprint"],
                    ANALYZER_VERSION, PROMPT_VERSION, "exact/model",
                )
                if compatible is not None:
                    continue
                active = self.repository.create_scan(
                    record["source_id"], "scan", ANALYZER_VERSION, PROMPT_VERSION, "exact/model",
                )
            self.repository.update_scan(
                active["scan_id"], "completed",
                transcript_id=self.transcripts[record["source_id"]]["transcript_id"],
            )
        preview = self.service.batch_preview()
        self.assertEqual(preview["will_evaluate"], 0)
        self.assertEqual(preview["already_current"], 4)

    def test_model_and_source_fingerprint_changes_use_normal_compatibility(self) -> None:
        current_record = next(record for record in self.records if record["filename"] == "current.mp4")
        self.cfg.LM_STUDIO_MOMENT_MODEL_ID = "changed/model"
        preview = self.service.batch_preview()
        current_item = next(item for item in preview["sources"] if item["source_id"] == current_record["source_id"])
        self.assertEqual(current_item["disposition"], "would_queue")

        self.cfg.LM_STUDIO_MOMENT_MODEL_ID = "exact/model"
        path = self.vods / "current.mp4"
        path.write_bytes(b"changed-source-fingerprint")
        preview = self.service.batch_preview()
        changed_item = next(item for item in preview["sources"] if item["filename"] == "current.mp4")
        self.assertEqual(changed_item["disposition"], "needs_check")

    def test_prompt_change_queues_a_previously_current_source(self) -> None:
        current_record = next(record for record in self.records if record["filename"] == "current.mp4")
        current_scan = self.repository.current_scan(current_record["source_id"])
        with self.repository.transaction() as db:
            db.execute(
                "UPDATE scans SET prompt_version=? WHERE scan_id=?",
                ("modscan-prompt-old", current_scan["scan_id"]),
            )
        preview = self.service.batch_preview()
        current_item = next(item for item in preview["sources"] if item["source_id"] == current_record["source_id"])
        self.assertEqual(current_item["disposition"], "would_queue")

    def test_changed_transcript_fingerprint_queues_a_previously_current_source(self) -> None:
        current_record = next(record for record in self.records if record["filename"] == "current.mp4")
        transcript_path = Path(self.transcripts[current_record["source_id"]]["cache_path"])
        write_transcript_atomic(transcript_path, {
            "segments": [{"start": 0.0, "end": 20.0, "text": "changed authoritative transcript"}],
        })
        preview = self.service.batch_preview()
        current_item = next(item for item in preview["sources"] if item["source_id"] == current_record["source_id"])
        self.assertEqual(current_item["disposition"], "already_current")
        launched = self.service.start_batch()
        self.service.prepare_batch(launched["batch"]["batch_id"])
        item = next(
            item for item in self.repository.batch_items(launched["batch"]["batch_id"])
            if item["source_id"] == current_record["source_id"]
        )
        self.assertEqual(item["disposition"], "queued")

    def test_launch_does_not_call_slow_hash_or_ffprobe(self) -> None:
        with mock.patch(
            "clipper_app.modular_scanner.service.source_record",
            side_effect=AssertionError("hashing must be asynchronous"),
        ), mock.patch(
            "clipper_app.modular_scanner.media.probe_duration",
            side_effect=AssertionError("ffprobe must be asynchronous"),
        ):
            started = time.monotonic()
            launched = self.service.start_batch()
        self.assertLess(time.monotonic() - started, 0.25)
        self.assertEqual(launched["batch"]["status"], "preparing")

    def test_preview_is_metadata_only_and_new_source_work_runs_during_preparation(self) -> None:
        new_path = self.vods / "new.mp4"
        new_path.write_bytes(b"new source")
        with mock.patch(
            "clipper_app.modular_scanner.service.source_record", wraps=source_record,
        ) as fingerprint, mock.patch(
            "clipper_app.modular_scanner.media.probe_duration", return_value=40.0,
        ) as ffprobe:
            preview = self.service.batch_preview()
            launched = self.service.start_batch()
            self.assertEqual(fingerprint.call_count, 0)
            self.assertEqual(ffprobe.call_count, 0)
            self.assertEqual(preview["needs_check"], 1)
            self.service.prepare_batch(launched["batch"]["batch_id"])
        self.assertEqual(fingerprint.call_count, 1)
        self.assertEqual(ffprobe.call_count, 1)

    def test_preparation_reuses_known_source_hashes_and_durations(self) -> None:
        batch_id = self.service.start_batch()["batch"]["batch_id"]
        with mock.patch(
            "clipper_app.modular_scanner.service.source_record",
            side_effect=AssertionError("known source was rehashed"),
        ), mock.patch(
            "clipper_app.modular_scanner.media.probe_duration",
            side_effect=AssertionError("known source was reprobed"),
        ):
            self.service.prepare_batch(batch_id)
        self.assertEqual(self.service.batch_status(batch_id)["checked"], 4)

    def test_preparation_failure_does_not_abort_later_sources(self) -> None:
        launched = self.service.start_batch()
        original = self.service._source_for_path

        def prepare(path, *, include_duration):
            if path.name == "stale.mp4":
                raise OSError("fingerprint failed")
            return original(path, include_duration=include_duration)

        with mock.patch.object(self.service, "_source_for_path", side_effect=prepare):
            self.service.prepare_batch(launched["batch"]["batch_id"])
        status = self.service.batch_status(launched["batch"]["batch_id"])
        self.assertEqual(status["failed"], 1)
        self.assertTrue(any(
            item["filename"] == "unscanned.mp4" and item["disposition"] == "queued"
            for item in self.repository.batch_items(launched["batch"]["batch_id"])
        ))

    def test_preparation_persists_incremental_progress_for_polling(self) -> None:
        batch_id = self.service.start_batch()["batch"]["batch_id"]
        original = self.service._source_for_path
        blocked = threading.Event()
        release = threading.Event()

        def prepare(path, *, include_duration):
            if path.name == "current.mp4":
                blocked.set()
                self.assertTrue(release.wait(2.0))
            return original(path, include_duration=include_duration)

        with mock.patch.object(self.service, "_source_for_path", side_effect=prepare):
            thread = threading.Thread(target=self.service.prepare_batch, args=(batch_id,))
            thread.start()
            self.assertTrue(blocked.wait(2.0))
            status = self.service.batch_status(batch_id)
            self.assertEqual(status["status"], "preparing")
            self.assertEqual((status["discovered"], status["checked"], status["checking"]), (4, 1, 1))
            release.set()
            thread.join(3.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.service.batch_status(batch_id)["checked"], 4)

    def test_restart_resumes_partially_prepared_batch_without_duplicate_scan(self) -> None:
        batch_id = self.service.start_batch()["batch"]["batch_id"]
        stale = next(record for record in self.records if record["filename"] == "stale.mp4")
        scan, reused = self.service.start_scan(stale["source_id"])
        self.assertFalse(reused)
        self.repository.add_batch_item(batch_id, {
            "source_id": stale["source_id"], "scan_id": scan["scan_id"], "disposition": "queued",
        })
        self.repository.update_batch(batch_id, "preparing", discovered_count=4)
        restarted = ModularScannerService(
            self.cfg, repository=self.repository, analyzer_factory=lambda: mock.Mock(),
            production_active=lambda: False, start_worker=False,
        )
        try:
            restarted.prepare_batch(batch_id)
            self.assertEqual(len(self.repository.list_scans(stale["source_id"])), 2)
            self.assertEqual(restarted.batch_status(batch_id)["checked"], 4)
        finally:
            restarted.close()

    def test_single_worker_continues_after_each_failed_batch_scan(self) -> None:
        launched = self.service.start_batch()
        batch_id = launched["batch"]["batch_id"]
        self.service.start_worker()
        worker = self.service._worker
        self.service.start_worker()
        self.assertIs(self.service._worker, worker)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            status = self.service.batch_status(batch_id)
            if status["status"] == "completed_with_failures":
                break
            time.sleep(0.02)
        queued_ids = [
            item["scan_id"] for item in self.repository.batch_items(batch_id)
            if item["disposition"] == "queued"
        ]
        self.assertGreaterEqual(len(queued_ids), 2)
        self.assertTrue(all(self.repository.get_scan(scan_id)["status"] == "failed" for scan_id in queued_ids))
        status = self.service.batch_status(batch_id)
        self.assertEqual((status["failed"], status["remaining"]), (len(queued_ids), 0))


if __name__ == "__main__":
    unittest.main()
