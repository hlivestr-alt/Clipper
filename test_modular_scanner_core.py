from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from clipper_app.modular_scanner.constants import ANALYZER_VERSION, PROMPT_VERSION
from clipper_app.modular_scanner.repository import ScannerRepository
from clipper_app.modular_scanner.transcripts import build_windows, transcript_fingerprint
from clipper_app.modular_scanner.validation import deduplicate, validate_candidate


def transcript(*segments: tuple[float, float, str]) -> dict:
    return {"segments": [{"start": start, "end": end, "text": text} for start, end, text in segments]}


def window_for(value: dict) -> dict:
    return build_windows(value, character_budget=10_000, overlap_seconds=10)[0]


class ModularScannerCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repository = ScannerRepository(self.root / "modular_library.sqlite3")
        self.source = self.repository.upsert_source({
            "source_id": "source-1",
            "filename": "vod.mp4",
            "canonical_path": str(self.root / "vod.mp4"),
            "file_size": 123,
            "mtime_ns": 456,
            "content_fingerprint": "abc",
            "duration_seconds": 120.0,
        })

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_repository_enables_safety_pragmas_and_creates_schema(self) -> None:
        with closing(self.repository.connect()) as db:
            self.assertEqual(db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"media_sources", "transcripts", "scans", "segments", "scan_chunks", "scan_rejections"}.issubset(names))

    def test_scan_cache_requires_exact_compatibility(self) -> None:
        value = transcript((0, 20, "serum vitamin c bagus"))
        fingerprint = transcript_fingerprint(value)
        record = self.repository.add_transcript("source-1", "scanner", "cache.json", fingerprint)
        scan = self.repository.create_scan("source-1", "scan", ANALYZER_VERSION, PROMPT_VERSION, "exact/model")
        self.repository.update_scan(scan["scan_id"], "completed", transcript_id=record["transcript_id"])
        self.assertEqual(
            self.repository.compatible_scan("source-1", fingerprint, ANALYZER_VERSION, PROMPT_VERSION, "exact/model")["scan_id"],
            scan["scan_id"],
        )
        self.assertIsNone(self.repository.compatible_scan("source-1", fingerprint, ANALYZER_VERSION, PROMPT_VERSION, "other/model"))

    def test_rescan_generation_is_immutable_and_old_success_remains_current(self) -> None:
        first = self.repository.create_scan("source-1", "scan", ANALYZER_VERSION, PROMPT_VERSION, "model")
        self.repository.update_scan(first["scan_id"], "completed")
        second = self.repository.create_scan("source-1", "rescan", ANALYZER_VERSION, PROMPT_VERSION, "model")
        self.assertEqual(second["generation"], 2)
        self.assertEqual(self.repository.current_scan("source-1")["scan_id"], first["scan_id"])
        self.repository.update_scan(second["scan_id"], "failed", error="nope")
        self.assertEqual(self.repository.current_scan("source-1")["scan_id"], first["scan_id"])

    def test_product_and_role_are_exact_enums(self) -> None:
        value = transcript((0, 20, "serum vitamin c membantu kulit"))
        window = window_for(value)
        base = {"start_seconds": 0.0, "end_seconds": 20.0, "product": "serum", "role": "benefits", "confidence": 0.8, "reason": "Clear benefit"}
        valid, rejection = validate_candidate(base, window, 20.0)
        self.assertIsNotNone(valid)
        self.assertIsNone(rejection)
        for key, invalid in (("product", "Serum"), ("product", " serum"), ("role", "main"), ("role", "Benefits")):
            candidate = {**base, key: invalid}
            accepted, rejected = validate_candidate(candidate, window, 20.0)
            self.assertIsNone(accepted)
            self.assertIn(rejected.code, {"invalid_product", "invalid_role"})

    def test_duration_boundary_and_authoritative_reconstruction(self) -> None:
        value = transcript((0, 7.5, "serum vitamin c"), (7.5, 15.0, "membantu mencerahkan kulit"))
        window = window_for(value)
        accepted, _ = validate_candidate({
            "start_seconds": 0.0, "end_seconds": 15.0, "product": "serum", "role": "benefits",
            "confidence": 0.9, "reason": "Benefit",
        }, window, 15.0)
        self.assertEqual(accepted["duration_seconds"], 15.0)
        self.assertEqual(accepted["transcript_text"], "serum vitamin c membantu mencerahkan kulit")
        repaired, reason = validate_candidate({
            "start_seconds": 0.0, "end_seconds": 14.999, "product": "serum", "role": "benefits",
            "confidence": 0.9, "reason": "Benefit",
        }, window, 15.0)
        self.assertEqual(repaired["duration_seconds"], 15.0)
        self.assertIsNone(reason)

    def test_source_window_bounds_and_product_evidence(self) -> None:
        window = window_for(transcript((10, 30, "toner membantu melembapkan")))
        base = {"start_seconds": 10.0, "end_seconds": 30.0, "product": "toner", "role": "benefits", "confidence": 0.7, "reason": "Benefit"}
        self.assertIsNotNone(validate_candidate(base, window, 30.0)[0])
        self.assertEqual(validate_candidate({**base, "start_seconds": 9.0}, window, 30.0)[1].code, "window_bounds")
        self.assertEqual(validate_candidate({**base, "end_seconds": 31.0}, window, 30.0)[1].code, "source_bounds")
        self.assertEqual(validate_candidate({**base, "product": "serum"}, window, 30.0)[1].code, "conflicting_product")
        conflict_window = window_for(transcript((0, 20, "toner dan serum tersedia")))
        self.assertEqual(validate_candidate({**base, "start_seconds": 0.0, "end_seconds": 20.0}, conflict_window, 20.0)[1].code, "conflicting_product")

    def test_deduplication_prefers_confidence_without_merging(self) -> None:
        common = {"product": "serum", "role": "hook", "transcript_text": "serum", "reason": "x", "_product_evidence": 1, "_coverage": 1.0}
        low = {**common, "start_seconds": 0.0, "end_seconds": 20.0, "duration_seconds": 20.0, "confidence": 0.6, "_order": 0}
        high = {**common, "start_seconds": 1.0, "end_seconds": 21.0, "duration_seconds": 20.0, "confidence": 0.9, "_order": 1}
        result = deduplicate([low, high])
        self.assertEqual(len(result), 1)
        self.assertEqual((result[0]["start_seconds"], result[0]["end_seconds"]), (1.0, 21.0))

    def test_long_transcript_windows_are_bounded_overlapping_and_absolute(self) -> None:
        value = transcript(*[(index * 5.0, index * 5.0 + 5.0, f"serum segment {index} " + "x" * 80) for index in range(30)])
        windows = build_windows(value, character_budget=700, overlap_seconds=15)
        self.assertGreater(len(windows), 1)
        self.assertGreater(windows[0]["end"], windows[1]["start"])
        self.assertIn("[", windows[1]["text"])
        self.assertGreater(windows[1]["start"], 0)
        self.assertLessEqual(max(len(item["text"]) for item in windows), 800)
        for left, right in zip(windows, windows[1:]):
            self.assertAlmostEqual(left["ownership_end"], right["ownership_start"])


if __name__ == "__main__":
    unittest.main()
