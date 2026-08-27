from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from clipper_app.modular_scanner.constants import ANALYZER_VERSION, PROMPT_VERSION
from clipper_app.modular_scanner.media import source_record
from clipper_app.modular_scanner.repository import ScannerRepository
from clipper_app.modular_scanner.service import ModularScannerService
from clipper_app.modular_scanner.transcripts import copy_production_transcript, transcript_fingerprint, write_transcript_atomic


class FakeAnalyzer:
    def __init__(self, responses: list[list[dict]], on_call=None):
        self.responses = responses
        self.calls = 0
        self.on_call = on_call

    def analyze(self, _window):
        result = self.responses[self.calls]
        self.calls += 1
        if self.on_call:
            self.on_call(self.calls)
        return result


class ModularScannerExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.vods = self.root / "vods"
        self.working = self.root / "working"
        self.vods.mkdir()
        self.working.mkdir()
        self.vod = self.vods / "source.mp4"
        self.vod.write_bytes(b"scanner-vod-content")
        self.cfg = SimpleNamespace(
            WORKING_DIR=str(self.working),
            QUEUE_INPUT_DIR=str(self.vods),
            QUEUE_STATE_FILE=str(self.working / "queue.json"),
            QUEUE_CONTROL_FILE=str(self.working / "control.json"),
            QUEUE_FOREVER_STATE_FILE=str(self.working / "forever.json"),
            MODSCAN_ENABLED=True,
            LM_STUDIO_MOMENT_MODEL_ID="exact/model",
        )
        self.repository = ScannerRepository(self.working / "modular_library.sqlite3")
        source = source_record(self.vod, include_duration=False)
        source["duration_seconds"] = 50.0
        self.source = self.repository.upsert_source(source)
        self.transcript = {
            "segments": [
                {"start": 0.0, "end": 20.0, "text": "serum vitamin c membantu mencerahkan kulit"},
                {"start": 20.0, "end": 40.0, "text": "serum ini punya manfaat untuk kulit kusam"},
            ]
        }
        self.transcript_path = self.working / "modular_scanner" / "transcripts" / self.source["source_id"] / "transcript.json"
        write_transcript_atomic(self.transcript_path, self.transcript)
        self.transcript_record = self.repository.add_transcript(
            self.source["source_id"], "scanner", str(self.transcript_path), transcript_fingerprint(self.transcript),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def candidate(self, start=0.0, end=20.0, confidence=0.8):
        return {
            "start_seconds": start,
            "end_seconds": end,
            "product": "serum",
            "role": "benefits",
            "confidence": confidence,
            "reason": "Reusable serum benefit",
        }

    def service(self, analyzer, **kwargs) -> ModularScannerService:
        return ModularScannerService(
            self.cfg,
            repository=self.repository,
            analyzer_factory=lambda: analyzer,
            start_worker=False,
            **kwargs,
        )

    def test_scan_completes_and_normal_scan_reuses_exact_cache(self) -> None:
        analyzer = FakeAnalyzer([[self.candidate()]])
        service = self.service(analyzer, production_active=lambda: False)
        scan, reused = service.start_scan(self.source["source_id"])
        self.assertFalse(reused)
        service.run_scan(scan["scan_id"])
        completed = service.get_scan(scan["scan_id"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["accepted_count"], 1)
        cached, reused = service.start_scan(self.source["source_id"])
        self.assertTrue(reused)
        self.assertEqual(cached["scan_id"], scan["scan_id"])
        self.assertEqual(analyzer.calls, 1)

    def test_rescan_preserves_old_current_until_new_success(self) -> None:
        analyzer = FakeAnalyzer([[self.candidate()], [self.candidate(confidence=0.9)]])
        service = self.service(analyzer, production_active=lambda: False)
        first, _ = service.start_scan(self.source["source_id"])
        service.run_scan(first["scan_id"])
        second, reused = service.start_scan(self.source["source_id"], rescan=True)
        self.assertFalse(reused)
        self.assertEqual(second["generation"], 2)
        self.assertTrue(service.get_scan(first["scan_id"])["is_current"])
        service.run_scan(second["scan_id"])
        self.assertTrue(service.get_scan(second["scan_id"])["is_current"])
        self.assertFalse(service.get_scan(first["scan_id"])["is_current"])

    def test_waits_for_production_and_automatically_continues(self) -> None:
        states = iter([True, True, False])
        observed = []
        service = self.service(
            FakeAnalyzer([[self.candidate()]]),
            production_active=lambda: next(states, False),
            sleep=lambda _seconds: observed.append(service.get_scan(scan["scan_id"])["status"]),
            wait_poll_seconds=0,
        )
        scan, _ = service.start_scan(self.source["source_id"])
        service.run_scan(scan["scan_id"])
        self.assertEqual(observed, ["waiting_for_production", "waiting_for_production"])
        self.assertEqual(service.get_scan(scan["scan_id"])["status"], "completed")

    def test_request_finishes_then_checkpoints_before_waiting_for_next(self) -> None:
        state = {"active": False}
        checkpoint_counts = []
        windows = [
            {"index": 0, "start": 0.0, "end": 20.0, "ownership_start": 0.0, "ownership_end": 20.0, "text": "[0-20] serum", "segments": self.transcript["segments"][:1]},
            {"index": 1, "start": 20.0, "end": 40.0, "ownership_start": 20.0, "ownership_end": 40.0, "text": "[20-40] serum", "segments": self.transcript["segments"][1:]},
        ]

        def on_call(count):
            if count == 1:
                state["active"] = True

        analyzer = FakeAnalyzer([[self.candidate()], [self.candidate(20.0, 40.0)]], on_call=on_call)
        service = self.service(
            analyzer,
            production_active=lambda: state["active"],
            sleep=lambda _seconds: (checkpoint_counts.append(sum(row["status"] == "completed" for row in self.repository.chunks(scan["scan_id"]))), state.update(active=False)),
            wait_poll_seconds=0,
        )
        scan, _ = service.start_scan(self.source["source_id"], rescan=True)
        with mock.patch("clipper_app.modular_scanner.service.build_windows", return_value=windows):
            service.run_scan(scan["scan_id"])
        self.assertEqual(checkpoint_counts, [1])
        self.assertEqual(analyzer.calls, 2)
        self.assertEqual(service.get_scan(scan["scan_id"])["status"], "completed")

    def test_restart_recovers_incomplete_scan_and_skips_completed_chunk(self) -> None:
        windows = [
            {"index": 0, "start": 0.0, "end": 20.0, "ownership_start": 0.0, "ownership_end": 20.0, "text": "first", "segments": self.transcript["segments"][:1]},
            {"index": 1, "start": 20.0, "end": 40.0, "ownership_start": 20.0, "ownership_end": 40.0, "text": "second", "segments": self.transcript["segments"][1:]},
        ]
        scan = self.repository.create_scan(self.source["source_id"], "scan", ANALYZER_VERSION, PROMPT_VERSION, "exact/model")
        self.repository.update_scan(scan["scan_id"], "analyzing", transcript_id=self.transcript_record["transcript_id"])
        self.repository.upsert_chunks(scan["scan_id"], windows)
        self.repository.complete_chunk(scan["scan_id"], 0, [self.candidate()])
        analyzer = FakeAnalyzer([[self.candidate(20.0, 40.0)]])
        service = self.service(analyzer, production_active=lambda: False)
        self.assertEqual(service.get_scan(scan["scan_id"])["status"], "queued")
        with mock.patch("clipper_app.modular_scanner.service.build_windows", return_value=windows):
            service.run_scan(scan["scan_id"])
        self.assertEqual(analyzer.calls, 1)
        self.assertEqual(service.get_scan(scan["scan_id"])["status"], "completed")

    def test_compatible_production_transcript_is_copied_not_modified(self) -> None:
        production_dir = self.working / self.vod.stem
        production_path = production_dir / "transcript.json"
        production = {
            **self.transcript,
            "metadata": {"source_video_path": str(self.vod.resolve())},
        }
        write_transcript_atomic(production_path, production)
        original = production_path.read_bytes()
        target = self.working / "modular_scanner" / "transcripts" / "copied" / "transcript.json"
        with mock.patch("transcriber.transcript_cache_is_compatible", return_value=True), mock.patch(
            "stage_cache.stage_fingerprint_matches", return_value=True
        ):
            copied = copy_production_transcript(self.source, self.cfg, target)
        self.assertIsNotNone(copied)
        self.assertTrue(target.is_file())
        self.assertEqual(production_path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
