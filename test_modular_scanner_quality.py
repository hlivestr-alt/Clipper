from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from clipper_app.modular_scanner.analyzer import SYSTEM_PROMPT
from clipper_app.modular_scanner.constants import ANALYZER_VERSION, PROMPT_VERSION
from clipper_app.modular_scanner.repository import ScannerRepository
from clipper_app.modular_scanner.service import ModularScannerService
from clipper_app.modular_scanner.transcripts import build_windows
from clipper_app.modular_scanner.validation import product_evidence, validate_candidate


def transcript(*segments: tuple[float, float, str]) -> dict:
    return {"segments": [{"start": start, "end": end, "text": text} for start, end, text in segments]}


def one_window(value: dict) -> dict:
    return build_windows(value, character_budget=100_000, overlap_seconds=10.0)[0]


def candidate(product: str, start: float, end: float, *, role: str = "benefits") -> dict:
    return {
        "start_seconds": start,
        "end_seconds": end,
        "product": product,
        "role": role,
        "confidence": 0.78,
        "reason": "Focused regression candidate",
    }


class SequenceAnalyzer:
    def __init__(self, responses: list[list[dict]]):
        self.responses = responses
        self.calls: list[dict] = []

    def analyze(self, window):
        self.calls.append(window)
        return self.responses[len(self.calls) - 1]


class ModularScannerEvidenceQualityTests(unittest.TestCase):
    def test_indonesian_suffixes_and_asr_aliases_are_transcript_evidence_only(self) -> None:
        cases = {
            "hydrating tonernya": "toner",
            "serumnya membantu kulit": "serum",
            "facial cleansernya lembut": "cleanser",
            "pakai air krim untuk mata": "eye_cream",
            "gunakan sheet mesh ini": "mask",
            "creamnya terasa lembut": "skin_cream",
            "krimnya membantu kulit": "skin_cream",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertGreater(product_evidence(text).get(expected, 0), 0)

        value = one_window(transcript((0, 20, "air krim membantu area mata")))
        for invalid in ("air krim", "tonernya"):
            accepted, rejection = validate_candidate(candidate(invalid, 0, 20), value, 20)
            self.assertIsNone(accepted)
            self.assertEqual(rejection.code, "invalid_product")

    def test_direct_and_bounded_context_evidence_with_conflict_rejection(self) -> None:
        context_window = one_window(transcript(
            (0, 5, "sekarang kita bahas hydrating tonernya"),
            (5, 25, "ini membantu kulit terasa lebih lembap dan segar"),
        ))
        accepted, rejection = validate_candidate(candidate("toner", 5, 25), context_window, 25)
        self.assertIsNotNone(accepted)
        self.assertIsNone(rejection)

        conflict = one_window(transcript((0, 20, "toner dan serum tersedia untuk kulit")))
        accepted, rejection = validate_candidate(candidate("toner", 0, 20), conflict, 20)
        self.assertIsNone(accepted)
        self.assertEqual(rejection.code, "conflicting_product")

    def test_usage_tutorial_is_not_cta_but_purchase_action_is(self) -> None:
        tutorial = one_window(transcript((0, 20, "facial cleansernya taruh di telapak, tambah air, foam, pijat lalu bilas")))
        accepted, rejection = validate_candidate(candidate("cleanser", 0, 20, role="cta"), tutorial, 20)
        self.assertIsNone(accepted)
        self.assertEqual(rejection.code, "role_not_supported")

        purchase = one_window(transcript((0, 20, "facial cleansernya lagi promo, klik keranjang kuning dan checkout sekarang")))
        accepted, rejection = validate_candidate(candidate("cleanser", 0, 20, role="cta"), purchase, 20)
        self.assertIsNotNone(accepted)
        self.assertIsNone(rejection)


class ModularScannerDurationQualityTests(unittest.TestCase):
    def test_near_miss_expands_to_smallest_coherent_authoritative_boundary(self) -> None:
        for short_end in (14.66, 13.0):
            with self.subTest(short_end=short_end):
                value = one_window(transcript(
                    (0, short_end, "serum vitamin c membantu mencerahkan kulit"),
                    (short_end, 16.2, "dan membuat kulit terasa lebih lembap"),
                ))
                accepted, rejection = validate_candidate(candidate("serum", 0, short_end), value, 16.2)
                self.assertIsNone(rejection)
                self.assertEqual(accepted["end_seconds"], 16.2)
                self.assertGreaterEqual(accepted["duration_seconds"], 15.0)
                repair = accepted["validation_diagnostics"]["duration_repair"]
                self.assertEqual(repair["outcome"], "expanded")

    def test_below_ten_seconds_is_not_repaired(self) -> None:
        value = one_window(transcript(
            (0, 9.9, "serum membantu kulit"),
            (9.9, 18, "kulit menjadi lebih lembap"),
        ))
        accepted, rejection = validate_candidate(candidate("serum", 0, 9.9), value, 18)
        self.assertIsNone(accepted)
        self.assertEqual(rejection.code, "duration_too_short")
        self.assertIn("not attempted", rejection.detail)

    def test_conflicting_product_and_unrelated_filler_cannot_pad_duration(self) -> None:
        cases = (
            ((0, 13, "serum membantu kulit"), (13, 17, "sekarang toner untuk wajah")),
            ((0, 13, "serum membantu kulit"), (13, 17, "terima kasih semuanya sampai jumpa")),
        )
        for first, extension in cases:
            with self.subTest(extension=extension[2]):
                value = one_window(transcript(first, extension))
                accepted, rejection = validate_candidate(candidate("serum", 0, 13), value, 17)
                self.assertIsNone(accepted)
                self.assertEqual(rejection.code, "duration_too_short")
                self.assertIn("repair failed", rejection.detail.casefold())


class ModularScannerWindowAndFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.repository = ScannerRepository(root / "scanner.sqlite3")
        self.cfg = SimpleNamespace(
            WORKING_DIR=str(root), QUEUE_INPUT_DIR=str(root), LM_STUDIO_MOMENT_MODEL_ID="test/model",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def service(self, analyzer) -> ModularScannerService:
        return ModularScannerService(
            self.cfg, repository=self.repository, analyzer_factory=lambda: analyzer,
            production_active=lambda: False, start_worker=False,
        )

    @staticmethod
    def long_transcript(text: str = "serum membantu kulit") -> dict:
        return transcript(*[(i * 30.0, (i + 1) * 30.0, text) for i in range(34)])

    def test_time_cap_splits_even_under_character_budget_with_absolute_ownership(self) -> None:
        windows = build_windows(self.long_transcript(), character_budget=1_000_000, overlap_seconds=45)
        self.assertGreater(len(windows), 1)
        self.assertTrue(all(window["end"] - window["start"] <= 900.0 for window in windows))
        self.assertGreater(windows[1]["start"], 0)
        self.assertIn(f"[{windows[1]['start']:.3f}-", windows[1]["text"])
        for left, right in zip(windows, windows[1:]):
            self.assertAlmostEqual(left["ownership_end"], right["ownership_start"])

    def test_product_rich_empty_result_subdivides_and_keeps_absolute_timestamps(self) -> None:
        window = build_windows(self.long_transcript(), character_budget=1_000_000)[0]
        recovered_candidate = candidate("serum", 60, 80)
        analyzer = SequenceAnalyzer([[], [recovered_candidate], [recovered_candidate]])
        self.repository.upsert_source({
            "source_id": "source", "filename": "vod.mp4", "canonical_path": str(Path(self.temp.name) / "vod.mp4"),
            "file_size": 1, "mtime_ns": 1, "content_fingerprint": "fingerprint", "duration_seconds": 900,
        })
        scan = self.repository.create_scan("source", "scan", ANALYZER_VERSION, PROMPT_VERSION, "test/model")
        result = self.service(analyzer)._analyze_with_empty_recovery(analyzer, window, scan["scan_id"])
        self.assertEqual(result, [recovered_candidate])
        self.assertEqual(len(analyzer.calls), 3)
        self.assertEqual(analyzer.calls[1]["start"], window["start"])
        self.assertGreater(analyzer.calls[2]["start"], 0)
        self.assertEqual(len(self.repository.list_scans("source")), 1)

    def test_product_empty_result_is_valid_without_subdivision(self) -> None:
        window = build_windows(self.long_transcript("host sedang menyapa penonton"), character_budget=1_000_000)[0]
        analyzer = SequenceAnalyzer([[]])
        self.assertEqual(self.service(analyzer)._analyze_with_empty_recovery(analyzer, window, "scan-id"), [])
        self.assertEqual(len(analyzer.calls), 1)

    def test_recursive_subdivision_is_bounded(self) -> None:
        window = build_windows(self.long_transcript(), character_budget=1_000_000)[0]
        analyzer = SequenceAnalyzer([[] for _ in range(20)])
        self.assertEqual(self.service(analyzer)._analyze_with_empty_recovery(analyzer, window, "scan-id"), [])
        self.assertLessEqual(len(analyzer.calls), 7)


class ModularScannerPromptAndPersistenceTests(unittest.TestCase):
    def test_prompt_covers_roles_duration_coverage_deduplication_and_confidence_bands(self) -> None:
        prompt = SYSTEM_PROMPT.casefold()
        for phrase in (
            "generic product description alone is not a hook",
            "usage/tutorial steps",
            "passing ingredient mention",
            "hook 15-25 seconds",
            "benefits 15-35 seconds",
            "ingredients 15-40 seconds",
            "0.90-1.00",
            "0.75-0.89",
            "0.60-0.74",
            "for each product independently",
            "same local discussion",
        ):
            self.assertIn(phrase, prompt)

    def test_versions_are_bumped_and_duration_diagnostics_are_persisted(self) -> None:
        self.assertEqual(ANALYZER_VERSION, "modscan-v2")
        self.assertEqual(PROMPT_VERSION, "modscan-prompt-v2")
        with tempfile.TemporaryDirectory() as directory:
            repository = ScannerRepository(Path(directory) / "scanner.sqlite3")
            source = repository.upsert_source({
                "source_id": "source", "filename": "vod.mp4", "canonical_path": str(Path(directory) / "vod.mp4"),
                "file_size": 1, "mtime_ns": 1, "content_fingerprint": "fingerprint", "duration_seconds": 20,
            })
            scan = repository.create_scan("source", "scan", ANALYZER_VERSION, PROMPT_VERSION, "model")
            value = one_window(transcript(
                (0, 13, "serum membantu kulit"), (13, 16, "kulit menjadi lebih lembap"),
            ))
            accepted, _ = validate_candidate(candidate("serum", 0, 13), value, 20)
            repository.replace_segments(scan["scan_id"], source, [accepted])
            with closing(sqlite3.connect(repository.path)) as db:
                raw = db.execute("SELECT validation_diagnostics_json FROM segments").fetchone()[0]
            self.assertEqual(json.loads(raw)["duration_repair"]["outcome"], "expanded")

    def test_v1_scanner_database_migrates_without_losing_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scanner.sqlite3"
            repository = ScannerRepository(path)
            source = repository.upsert_source({
                "source_id": "source", "filename": "vod.mp4", "canonical_path": str(Path(directory) / "vod.mp4"),
                "file_size": 1, "mtime_ns": 1, "content_fingerprint": "fingerprint", "duration_seconds": 20,
            })
            scan = repository.create_scan("source", "scan", "modscan-v1", "modscan-prompt-v1", "model")
            segment = {
                **candidate("serum", 0, 20), "duration_seconds": 20, "transcript_text": "serum membantu kulit",
                "validation_diagnostics": {},
            }
            repository.replace_segments(scan["scan_id"], source, [segment])
            with closing(sqlite3.connect(path)) as db:
                db.execute("ALTER TABLE segments DROP COLUMN validation_diagnostics_json")
                db.execute("UPDATE schema_meta SET version=1")
                db.commit()

            migrated = ScannerRepository(path)
            self.assertEqual([item["scan_id"] for item in migrated.list_scans("source")], [scan["scan_id"]])
            self.assertEqual(len(migrated.list_segments(scan["scan_id"])), 1)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("SELECT version FROM schema_meta").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
